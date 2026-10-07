"""Managed scope — IT-pushed, user-immutable config & env layer.

DISTINCT from ``hermes_cli.config.is_managed()`` / ``HERMES_MANAGED`` (a coarse package-manager
write-lock that blocks all mutation); this layer injects specific immutable values. The two are
independent and may coexist. v1 enforcement is filesystem permissions only (see
``docs/design/managed-scope.md`` §7); ``get_managed_dir()`` is the single seam for adding
macOS / Windows native locations later.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Dict, Mapping, Optional

import yaml

# Stale-module bridge: this module binds ``utils.file_signature`` at import time, so a fresh
# import in a post-pull updater process (pre-handoff purge keeps root modules cached) dies
# unless the stale ``utils`` is dropped first. See hermes_cli.stale_modules.
from hermes_cli.stale_modules import drop_stale_root_modules

drop_stale_root_modules()

from utils import file_signature

logger = logging.getLogger(__name__)

# POSIX default. Other-platform locations belong ONLY inside get_managed_dir().
_DEFAULT_MANAGED_DIR = Path("/etc/hermes")


class ManagedConfigError(Exception):
    """The managed ``config.yaml`` exists but is unreadable, unparseable, or not a mapping.

    Raised ONLY in strict mode (``strict=True``) — the mode the gateway's live-reload
    consumers use. There, a malformed/truncated intermediate file must fail the reload
    attempt (the active runtime keeps running) instead of silently dropping the
    administrator's pins and letting user values through. The message is deliberately
    content-free: parser exceptions embed YAML snippets, and managed file contents —
    which may shape secrets — never reach a log from this path.
    """

_CACHE_LOCK = threading.Lock()
# path_key -> (*file_signature, parsed)
_CONFIG_CACHE: Dict[str, tuple] = {}
_ENV_CACHE: Dict[str, tuple] = {}


def _under_pytest() -> bool:
    """True inside the test suite: ignore the system ``/etc/hermes`` so a real managed scope on a
    dev/CI box can't leak policy into the suite. An explicit ``HERMES_MANAGED_DIR`` still wins."""
    return "PYTEST_CURRENT_TEST" in os.environ


def get_managed_dir() -> Optional[Path]:
    """Resolve the managed-scope directory, or None when no scope is present.

    Priority: ``$HERMES_MANAGED_DIR`` (IT-only bootstrap override; never persisted to any .env;
    honored only when non-empty AND the directory exists), then ``/etc/hermes`` when it exists.
    A missing directory resolves to None — the common case, so it must be cheap + side-effect-free.
    """
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override:
        p = Path(override)
    elif _under_pytest():
        return None
    else:
        p = _DEFAULT_MANAGED_DIR
    return p if p.is_dir() else None


def invalidate_managed_cache() -> None:
    """Drop cached managed config/env. For tests and post-edit reloads."""
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _ENV_CACHE.clear()


def _cached_read(path: Path, cache: Dict[str, tuple], parse):
    """Shared stat-signature-keyed read; returns a deepcopy of the parsed value.

    ``None`` when the file is absent or fails to parse (fail-open). A parse failure is logged
    LOUDLY — the admin needs to know their policy isn't applied — but never raises, so a malformed
    managed file can't brick startup.
    """
    try:
        st = path.stat()
    except OSError:
        return None  # absent
    key = file_signature(st)
    path_key = str(path)
    with _CACHE_LOCK:
        hit = cache.get(path_key)
        if hit is not None and hit[:len(key)] == key:
            return copy.deepcopy(hit[len(key)])
    try:
        parsed = parse(path)
    except Exception as exc:  # noqa: BLE001 — fail-open, but LOUD
        logger.warning(
            "managed scope: failed to parse %s: %s — IGNORING this managed file. "
            "Admin policy from this file is NOT being applied. Fix and restart.",
            path, exc)
        return None
    with _CACHE_LOCK:
        cache[path_key] = (*key, copy.deepcopy(parsed))
    return parsed


def _load_managed_file(
    name: str, cache: Dict[str, tuple], parse, managed_dir: Optional[Path] = None,
) -> dict:
    if managed_dir is None:
        managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    parsed = _cached_read(managed_dir / name, cache, parse)
    return parsed if isinstance(parsed, dict) else {}


def load_managed_config(managed_dir: Optional[Path] = None, *, strict: bool = False) -> dict:
    """Parsed managed config.yaml, or {} when absent/malformed (fail-open).

    ``managed_dir`` pins an explicit scope directory: the gateway's live-reload
    watchers capture it at connect time (inside the owning profile's scope) so a
    poll-time env flip can never redirect the read. The default resolves the
    scope fresh, exactly as before.

    ``strict=True`` is the live-reload mode: instead of failing open it raises
    ``ManagedConfigError`` when the file EXISTS but is unreadable, unparseable,
    or not a mapping (null/empty/list/scalar — an editor's truncate-write passes
    through exactly these shapes), so a broken intermediate edit retains the
    active runtime rather than discarding the administrator's pins. An absent
    file stays ``{}`` in both modes (the administrator deliberately removed the
    policy). Startup callers keep the default fail-open behavior.
    """
    if strict:
        return _load_managed_config_strict(managed_dir)
    return _load_managed_file(
        "config.yaml", _CONFIG_CACHE,
        lambda p: yaml.safe_load(p.read_text(encoding="utf-8-sig")) or {},
        managed_dir=managed_dir,
    )


def _load_managed_config_strict(managed_dir: Optional[Path]) -> dict:
    """Strict-mode read for live consumers; see ``load_managed_config(strict=True)``.

    Deliberately NOT routed through ``_cached_read``: that path shares its cache with
    the fail-open consumers (which store ``{}`` for exactly the broken shapes strict
    mode must reject), and a live watcher only pays this read on a stat change, so
    caching buys nothing. Raises ``ManagedConfigError`` with a fixed, content-free
    message; never logs (the caller logs its own fixed warning).
    """
    if managed_dir is None:
        managed_dir = get_managed_dir()
    if managed_dir is None:
        return {}
    path = managed_dir / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}  # absent: the administrator removed the policy — same as startup
    except (OSError, UnicodeError):
        raise ManagedConfigError(f"managed scope: {path} is unreadable") from None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        raise ManagedConfigError(f"managed scope: {path} is not valid YAML") from None
    if not isinstance(parsed, dict):
        # null/empty/list/scalar — mid-write shapes, never a deliberate policy.
        raise ManagedConfigError(f"managed scope: {path} is not a mapping") from None
    return parsed


def managed_config_env_snapshot(managed_dir: Optional[Path] = None) -> Dict[str, str]:
    """``{VAR: value}`` for every env-backed ``${...}`` ref in the managed config, NOW.

    Live-reload consumers call this ONCE inside connect()'s profile scope — where
    ``_env_ref_snapshot`` resolves through the active profile's secret scope — and
    hand the mapping back via ``apply_managed_overlay(env=...)``, so a background
    poll never reads the process environment or a secret (which may name another
    profile by then). Refs absent from the mapping stay verbatim at expansion
    (unresolvable without an env read; the block then fails validation, fail closed).
    """
    from hermes_cli.config import _env_ref_snapshot

    snapshot = _env_ref_snapshot(load_managed_config(managed_dir=managed_dir))
    return {name: value for name, value in snapshot.items() if value is not None}


def _expand_env_vars_with(obj, env: Mapping[str, str]):
    """``hermes_cli.config._expand_env_vars`` against an EXPLICIT mapping, nothing else.

    Same ref shapes (``${VAR}`` / ``${env:VAR}``), same unresolved-stays-verbatim
    rule — but the lookup never touches ``os.environ`` or the secret scope, so a
    background poll expands exactly the environment its caller captured.
    """
    from hermes_cli.config import _ENV_REF_RE, _env_ref_var_name

    if isinstance(obj, str):
        def repl(match) -> str:
            name = _env_ref_var_name(match.group(1))
            if name is None:
                return match.group(0)
            value = env.get(name)
            return value if value is not None else match.group(0)

        return _ENV_REF_RE.sub(repl, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars_with(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars_with(item, env) for item in obj]
    return obj


def load_managed_env() -> Dict[str, str]:
    """Parsed managed .env (KEY=VALUE), or {} when absent (fail-open)."""
    return _load_managed_file(".env", _ENV_CACHE, _parse_managed_env)


def _parse_managed_env(path: Path) -> Dict[str, str]:
    from agent.secret_scope import load_env_file

    path.read_text(encoding="utf-8-sig")  # load_env_file swallows decode errors; an admin file must fail LOUD
    return load_env_file(path)


def apply_managed_overlay(
    config: dict,
    managed_dir: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    strict: bool = False,
) -> dict:
    """Overlay administrator-pinned config values on top of an already-built dict.

    ``${VAR}`` refs in the managed config expand against the PROCESS env only, so a user cannot
    shadow a managed literal via a ref they control; a bare root ``model: x/y`` string is promoted
    to ``model.default`` so it can't clobber the dict shape callers expect; managed values
    deep-merge ON TOP per leaf while sibling keys stay user-controlled. Fail-open: returns
    ``config`` unchanged when no scope is present or on any error. Mutates and returns ``config``.
    ``managed_dir`` pins an explicit scope directory (see ``load_managed_config``); the default
    resolves the scope fresh.

    ``env`` (live-reload consumers) replaces the process-env read: refs expand against
    exactly this caller-captured mapping — a background poll never reads the mutable
    process environment or a secret. ``strict`` (same consumers) propagates
    ``ManagedConfigError`` for an existing-but-broken managed file instead of failing
    open, so a malformed intermediate edit can't silently drop the pins; the default
    keeps startup's fail-open semantics.
    """
    if strict:
        # ManagedConfigError propagates: a broken managed file must fail this reload
        # attempt, never fail open. Only the live watcher opts into this.
        managed = load_managed_config(managed_dir=managed_dir, strict=True)
    else:
        try:
            managed = load_managed_config(managed_dir=managed_dir)
        except Exception:  # noqa: BLE001 — overlay must never break a caller
            logger.warning("managed scope: failed to load managed config", exc_info=True)
            return config
    if not managed:
        return config
    try:
        # Imported lazily to avoid an import cycle (config imports managed_scope).
        from hermes_cli.config import _deep_merge, _expand_env_vars, _normalize_root_model_keys
        expanded = (
            _expand_env_vars_with(managed, env)
            if env is not None
            else _expand_env_vars(managed)
        )
        managed_expanded = _normalize_root_model_keys(expanded)
        # _normalize_root_model_keys only promotes the string when root provider/base_url
        # keys exist to migrate; handle the bare case here (matches cli.py) so _deep_merge
        # never replaces the caller's ``model`` dict with a string.
        if isinstance(managed_expanded.get("model"), str):
            managed_expanded = dict(managed_expanded)
            managed_expanded["model"] = {"default": managed_expanded["model"]}
        return _deep_merge(config, managed_expanded)
    except Exception:  # noqa: BLE001 — overlay must never break a caller
        logger.warning("managed scope: failed to apply config overlay", exc_info=True)
        return config


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys: set = set()
    for k, v in d.items():
        dotted = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            keys |= _flatten_keys(v, dotted)
        else:
            keys.add(dotted)
    return keys


def managed_config_keys() -> set:
    """Dotted leaf keys pinned by the managed config (e.g. {'model.default'})."""
    return _flatten_keys(load_managed_config())


def is_key_managed(dotted_key: str) -> bool:
    """True if the exact dotted config key is pinned by the managed layer."""
    return dotted_key in managed_config_keys()


def is_env_managed(name: str) -> bool:
    """True if the env var name is pinned by the managed .env layer."""
    return name in load_managed_env()
