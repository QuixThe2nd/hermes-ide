"""Discord-independent quota collection — collector state and snapshot contract.

This sibling owns the collection-only slice of quota_channels: it reuses the
provider metric/token helpers from ``core`` and the wallet identity helpers
from ``zai_wallets``, but never builds Discord headers, never reconciles
Discord channels, and never makes a Discord request. The Discord publishing
tick (``core.run_tick``) is untouched and remains the default CLI path.

Persistence: ``quota_channels_collector.json`` under HERMES_HOME holds
last-good readings per provider and per Z.AI wallet. A failed refresh keeps
the previous reading and records a sanitized error — a failure is never
persisted as zero quota. Wallet ordinals stay shared with the Discord tick
(``zai_wallet_ordinals`` in the legacy state file) so "z.ai N" means the same
credential in both surfaces; channel mappings are passed through untouched.

Read contract (consumed by the later web panel): ``read_snapshot`` returns a
plain-dict snapshot (``SNAPSHOT_SCHEMA_VERSION``) with absolute freshness
timestamps and reset horizons, per-entry status of fresh / stale / error /
unknown / disabled, and no credentials anywhere in the payload.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from plugins.quota_channels.core import (
    DEFAULT_QUOTA_INTERVAL_SECONDS,
    PROVIDER_SPECS,
    QUOTA_METRICS,
    TOKEN_FETCHERS,
    HttpFn,
    NowFn,
    QuotaChannelsError,
    _error_text,
    _hermes_home,
    _zai_quota_metrics,
    default_http,
    fetch_zai_tokens_7d,
    grok_reset_credits,
    load_state,
    quota_due,
    redact_secrets,
    save_wallet_state,
)
from plugins.quota_channels.zai_wallets import (
    assign_wallet_ordinals,
    enumerate_zai_wallets,
    pick_best_zai_wallet,
    redact_wallet_error,
    wallet_display_label,
    wallets_from_state_when_unreadable,
)

SNAPSHOT_SCHEMA_VERSION = 1
COLLECTOR_STATE_FILENAME = "quota_channels_collector.json"
# A last-good reading older than this many quota intervals renders as stale.
STALE_AFTER_INTERVALS = 2

_ENTRY_STATUSES = ("fresh", "stale", "error", "unknown", "disabled")


def collector_state_path() -> Path:
    return _hermes_home() / COLLECTOR_STATE_FILENAME


def _load_collector_state_raw() -> Tuple[Dict[str, Any], Optional[str]]:
    path = collector_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"cannot read {path.name}: {_error_text(exc)}"
    if not isinstance(raw, dict):
        return {}, f"cannot read {path.name}: state is not a JSON object"
    return raw, None


def load_collector_state() -> Dict[str, Any]:
    state, _ = _load_collector_state_raw()
    return state


def save_collector_state(state: Mapping[str, Any]) -> None:
    path = collector_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".quota-collector.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        raise QuotaChannelsError(f"cannot write {path}: {exc}") from exc


def _provider_secrets(key: str) -> Tuple[Any, ...]:
    """Credentials the collector may redact from provider errors; never raises."""
    try:
        if key == "codex":
            from plugins.quota_channels.core import load_store

            toks = (
                load_store().get("providers", {}).get("openai-codex", {}).get("tokens", {})
            )
            return (toks.get("access_token"), toks.get("refresh_token"))
        if key == "kimi":
            from plugins.quota_channels.core import kimi_api_key

            return (kimi_api_key(),)
        if key == "zai":
            from plugins.quota_channels.core import zai_api_key

            return (zai_api_key(),)
        if key == "cursor":
            from plugins.quota_channels.core import cursor_access_token

            return (cursor_access_token(),)
        if key == "grok":
            from plugins.quota_channels.core import load_store

            toks = (
                load_store().get("providers", {}).get("xai-oauth", {}).get("tokens", {})
            )
            return (toks.get("access_token"), toks.get("refresh_token"))
    except Exception:
        return ()
    return ()


def _normalize_metrics(
    key: str, raw: Any
) -> Tuple[Dict[str, Any], Any, Optional[str]]:
    """(data, resets, reset_error) from a QUOTA_METRICS result tuple.

    Mirrors run_provider_quota's unpacking: codex/zai may carry trailing
    ResetCredits and a reset-fetch error; cursor carries auto/api remaining.
    """
    resets = None
    reset_error: Optional[str] = None
    if key == "cursor":
        auto_remaining, api_remaining, reset_secs = raw
        data: Dict[str, Any] = {
            "pct": min(int(auto_remaining), int(api_remaining)),
            "reset_seconds": float(reset_secs),
            "cursor_auto_pct": int(auto_remaining),
            "cursor_api_pct": int(api_remaining),
        }
        return data, resets, reset_error
    if key in ("codex", "zai"):
        if len(raw) == 4:
            remaining, reset_secs, resets, reset_error = raw
        elif len(raw) == 3:
            remaining, reset_secs, resets = raw
        else:
            remaining, reset_secs = raw
    else:
        remaining, reset_secs = raw
    data = {"pct": int(remaining), "reset_seconds": float(reset_secs)}
    return data, resets, reset_error


def _auxiliary_error_secrets(
    key: str, api_key: Optional[str] = None
) -> Tuple[Any, ...]:
    secrets = list(_provider_secrets(key))
    if api_key:
        secrets.append(api_key)
    return tuple(s for s in secrets if s)


def _sanitize_auxiliary_error(
    key: str,
    message: Optional[str],
    *,
    api_key: Optional[str] = None,
    secrets_before: Tuple[Any, ...] = (),
) -> Optional[str]:
    if not message:
        return message
    combined = tuple(
        s
        for s in (*secrets_before, *_auxiliary_error_secrets(key, api_key))
        if s
    )
    return redact_secrets(message, combined)


def _apply_resets(data: Dict[str, Any], resets: Any, reset_error: Optional[str]) -> None:
    if reset_error:
        data["reset_error"] = reset_error
    if resets is None:
        return
    data["reset_count"] = int(resets.count)
    if resets.expiry_secs is not None:
        data["reset_expiry_seconds"] = float(resets.expiry_secs)
    if resets.expiry_horizons:
        # each credit's own expiry clock, relative to the collection time
        data["reset_expiry_horizons"] = [float(h) for h in resets.expiry_horizons]


def _collect_provider_data(
    key: str,
    *,
    http_fn: HttpFn,
    now_fn: NowFn,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Fresh quota reading for one provider (or one Z.AI wallet credential).

    Raises on failure; the caller redacts and retains last-good state.
    """
    secrets_before = _auxiliary_error_secrets(key, api_key)
    if api_key is not None:
        raw = _zai_quota_metrics(http_fn=http_fn, now_fn=now_fn, api_key=api_key)
    else:
        raw = QUOTA_METRICS[key](http_fn=http_fn, now_fn=now_fn)
    data, resets, reset_error = _normalize_metrics(key, raw)
    if key == "grok":
        resets, reset_error = grok_reset_credits(http_fn=http_fn, now_fn=now_fn)
    _apply_resets(data, resets, reset_error)
    if data.get("reset_error"):
        data["reset_error"] = _sanitize_auxiliary_error(
            key,
            data["reset_error"],
            api_key=api_key,
            secrets_before=secrets_before,
        )

    if api_key is not None:
        try:
            data["tokens_7d"] = fetch_zai_tokens_7d(
                http_fn=http_fn, now_fn=now_fn, api_key=api_key
            )
        except Exception as exc:
            data["token_error"] = redact_secrets(_error_text(exc), (api_key,))
    elif key in TOKEN_FETCHERS:
        try:
            data["tokens_7d"] = TOKEN_FETCHERS[key](http_fn=http_fn, now_fn=now_fn)
        except Exception as exc:
            data["token_error"] = redact_secrets(
                _error_text(exc), _provider_secrets(key)
            )
    return data


def _wallet_reading_from_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    reading: Dict[str, Any] = {
        "pct": int(data["pct"]),
        "reset_seconds": float(data["reset_seconds"]),
    }
    for field in ("reset_count", "reset_expiry_seconds", "reset_expiry_horizons"):
        if field in data:
            reading[field] = data[field]
    return reading


def run_collect(
    config: Mapping[str, Any],
    *,
    force: bool = False,
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> Dict[str, Any]:
    """One collection-only tick: refresh enabled providers and Z.AI wallets.

    No Discord token, channel IDs, renames, reconciliation or Discord HTTP of
    any kind. Interval-gated like the Discord tick (``quota_interval_seconds``
    with ``force`` bypass); the gate and the success timestamp are owned by the
    collector state file, never the Discord tick's ``last_quota_success``.
    """
    state = load_collector_state()
    interval = int(config["quota_interval_seconds"])
    now = float(now_fn())
    gate_state = {"last_quota_success": state.get("last_collect_success") or 0}
    if not quota_due(gate_state, interval, force, now_fn=now_fn):
        snapshot = read_snapshot(config, now_fn=now_fn)
        snapshot["did_collect"] = False
        return snapshot

    state["schema_version"] = SNAPSHOT_SCHEMA_VERSION
    state["last_collect_attempt"] = int(now)
    providers_state: Dict[str, Any] = dict(state.get("providers") or {})
    wallets_state: Dict[str, Any] = dict(state.get("wallets") or {})
    any_success = False

    legacy_state = load_state()
    enabled_keys = [key for key, _, _ in config["providers"]]
    zai_enabled = "zai" in enabled_keys

    wallets: List[Any] = []
    pool_unreadable = False
    ordinals: Dict[str, int] = {}
    if zai_enabled:
        hermes_home = _hermes_home()
        wallets, pool_unreadable = enumerate_zai_wallets(hermes_home)
        if pool_unreadable and not wallets:
            wallets = wallets_from_state_when_unreadable(legacy_state, hermes_home)
        state["zai_pool_unreadable"] = bool(pool_unreadable)
        if wallets:
            # Ordinal identity is shared with the Discord tick; the Discord
            # channel map is passed through untouched and no channel is
            # created, probed or deleted here. Ordinals of wallets that are
            # momentarily unenumerable (unresolved env reference, unreadable
            # pool) are preserved so their display number never flaps.
            ordinals, high_water = assign_wallet_ordinals(wallets, legacy_state)
            merged_ordinals = dict(legacy_state.get("zai_wallet_ordinals") or {})
            merged_ordinals.update(ordinals)
            ordinals = merged_ordinals
            save_wallet_state(
                dict(legacy_state.get("zai_wallet_channels") or {}),
                ordinals,
                high_water,
            )

    for key, _label, _channel_id in config["providers"]:
        if key == "zai":
            # wallets own z.ai collection (or the authoritative-empty skip),
            # exactly like the Discord tick
            continue
        entry = dict(providers_state.get(key) or {})
        entry["attempted_at"] = now
        try:
            data = _collect_provider_data(key, http_fn=http_fn, now_fn=now_fn)
        except Exception as exc:
            entry["last_error"] = redact_secrets(
                _error_text(exc), _provider_secrets(key)
            )
        else:
            entry["data"] = data
            entry["collected_at"] = now
            entry["last_error"] = None
            any_success = True
        providers_state[key] = entry

    if zai_enabled and wallets:
        for wallet in wallets:
            entry_id = wallet.entry_id
            wentry = dict(wallets_state.get(entry_id) or {})
            ordinal = ordinals.get(entry_id)
            if ordinal is not None:
                wentry["ordinal"] = int(ordinal)
                wentry["display"] = (
                    "z.ai"
                    if len(wallets) == 1
                    else wallet_display_label(int(ordinal))
                )
            wentry["attempted_at"] = now
            if not wallet.runtime_api_key:
                wentry["last_error"] = (
                    "wallet credential unavailable "
                    "(env reference unresolved or pool unreadable)"
                )
            else:
                try:
                    data = _collect_provider_data(
                        "zai",
                        http_fn=http_fn,
                        now_fn=now_fn,
                        api_key=wallet.runtime_api_key,
                    )
                except Exception as exc:
                    wentry["last_error"] = redact_wallet_error(exc, wallet)
                else:
                    wentry["data"] = data
                    wentry["collected_at"] = now
                    wentry["last_error"] = None
                    any_success = True
            wallets_state[entry_id] = wentry

        # legacy "z.ai" alias = best currently spendable active wallet
        active_ids = {wallet.entry_id for wallet in wallets}
        wallet_readings = {
            entry_id: _wallet_reading_from_data(wentry["data"])
            for entry_id, wentry in wallets_state.items()
            if entry_id in active_ids and isinstance(wentry.get("data"), Mapping)
        }
        from plugins.fallback_quota_reorder.reliability import rates_for_providers

        picked = pick_best_zai_wallet(
            wallet_readings, rates_for_providers(("zai",), now_fn=now_fn)
        )
        if picked is not None:
            selected_id, alias = picked
            source = wallets_state.get(selected_id) or {}
            zai_entry = dict(providers_state.get("zai") or {})
            zai_entry["attempted_at"] = source.get("attempted_at", now)
            zai_entry["data"] = {**alias}
            zai_entry["collected_at"] = source.get("collected_at")
            zai_entry["last_error"] = source.get("last_error")
            zai_entry["alias_wallet_entry_id"] = selected_id
            if source.get("display"):
                zai_entry["alias_wallet_display"] = source["display"]
            elif source.get("ordinal") is not None:
                zai_entry["alias_wallet_display"] = wallet_display_label(
                    int(source["ordinal"])
                )
            providers_state["zai"] = zai_entry

    state["providers"] = providers_state
    state["wallets"] = wallets_state
    if any_success:
        state["last_collect_success"] = int(now)
    save_collector_state(state)

    snapshot = read_snapshot(config, now_fn=now_fn)
    snapshot["did_collect"] = True
    return snapshot


def _absolute_data(
    data: Optional[Mapping[str, Any]], collected_at: Optional[float]
) -> Optional[Dict[str, Any]]:
    if not isinstance(data, Mapping):
        return None
    out = dict(data)
    if collected_at is not None:
        try:
            out["reset_at"] = float(collected_at) + float(data["reset_seconds"])
        except (KeyError, TypeError, ValueError):
            pass
        horizons = data.get("reset_expiry_horizons")
        if isinstance(horizons, (list, tuple)):
            out["reset_expiry_at"] = [
                float(collected_at) + float(h) for h in horizons
            ]
    return out


def _entry_snapshot(
    entry: Mapping[str, Any],
    *,
    now: float,
    stale_after: float,
) -> Dict[str, Any]:
    collected_at = entry.get("collected_at")
    attempted_at = entry.get("attempted_at")
    error = entry.get("last_error")
    data = entry.get("data") if isinstance(entry.get("data"), Mapping) else None
    age_seconds = (now - float(collected_at)) if collected_at else None
    stale = bool(age_seconds is not None and age_seconds > stale_after)
    if not collected_at or data is None:
        status = "error" if error else "unknown"
    elif attempted_at and float(attempted_at) > float(collected_at) and error:
        # the last attempt failed; data below is the retained last-good read
        status = "error"
    elif stale:
        status = "stale"
    else:
        status = "fresh"
    return {
        "status": status,
        "collected_at": collected_at,
        "attempted_at": attempted_at,
        "age_seconds": age_seconds,
        "stale": stale,
        "error": error,
        "data": _absolute_data(data, float(collected_at) if collected_at else None),
    }


def read_snapshot(
    config: Optional[Mapping[str, Any]] = None,
    *,
    now_fn: NowFn = time.time,
) -> Dict[str, Any]:
    """Current collector snapshot for display consumers (e.g. the web panel).

    Never raises: a missing or corrupt state file yields unknown entries plus
    a sanitized ``state_error``. Failed refreshes surface as ``error`` with
    the retained last-good data and its age — never as zero quota.
    """
    now = float(now_fn())
    state, state_error = _load_collector_state_raw()
    interval = int(
        (config or {}).get("quota_interval_seconds", DEFAULT_QUOTA_INTERVAL_SECONDS)
    )
    stale_after = float(interval * STALE_AFTER_INTERVALS)

    enabled_keys: Optional[List[str]] = None
    if config is not None:
        enabled_keys = [key for key, _, _ in config["providers"]]

    providers_state = state.get("providers")
    if not isinstance(providers_state, Mapping):
        providers_state = {}
    providers: Dict[str, Any] = {}
    for key, label in PROVIDER_SPECS:
        if enabled_keys is not None and key not in enabled_keys:
            stored = providers_state.get(key)
            entry = {
                "status": "disabled",
                "enabled": False,
                "collected_at": None,
                "attempted_at": None,
                "age_seconds": None,
                "stale": False,
                "error": None,
                "data": None,
            }
            if isinstance(stored, Mapping) and isinstance(stored.get("data"), Mapping):
                # retain last-good data for a provider the operator turned off
                entry["data"] = _absolute_data(
                    stored["data"],
                    float(stored["collected_at"]) if stored.get("collected_at") else None,
                )
                entry["collected_at"] = stored.get("collected_at")
            providers[key] = {"key": key, "label": label, **entry}
            continue
        stored = providers_state.get(key)
        entry = _entry_snapshot(
            stored if isinstance(stored, Mapping) else {},
            now=now,
            stale_after=stale_after,
        )
        providers[key] = {"key": key, "label": label, "enabled": True, **entry}

    wallets_state = state.get("wallets")
    if not isinstance(wallets_state, Mapping):
        wallets_state = {}
    wallet_entries: List[Dict[str, Any]] = []
    for entry_id, wentry in wallets_state.items():
        if not isinstance(wentry, Mapping):
            continue
        snap = _entry_snapshot(wentry, now=now, stale_after=stale_after)
        if enabled_keys is not None and "zai" not in enabled_keys:
            snap["status"] = "disabled"
        wallet_entries.append(
            {
                "entry_id": str(entry_id),
                "ordinal": wentry.get("ordinal"),
                "display": wentry.get("display") or str(entry_id),
                **snap,
            }
        )
    wallet_entries.sort(
        key=lambda item: (item["ordinal"] is None, item["ordinal"] or 0, item["entry_id"])
    )

    last_success = state.get("last_collect_success")
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now,
        "interval_seconds": interval,
        "stale_after_seconds": stale_after,
        "last_collect_attempt": state.get("last_collect_attempt"),
        "last_collect_success": last_success,
        "next_collect_due_at": (
            float(last_success) + interval if last_success else None
        ),
        "state_error": state_error,
        "providers": providers,
        "wallets": wallet_entries,
        "zai_pool_unreadable": bool(state.get("zai_pool_unreadable")),
    }
