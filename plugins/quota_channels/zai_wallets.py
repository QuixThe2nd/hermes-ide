"""Per-credential Z.AI wallet rows for the Discord Models quota wall."""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from plugins.fallback_quota_reorder.core import (
    QuotaReading,
    is_low_quota,
    score_provider,
)
from plugins.fallback_quota_reorder.reliability import ReliabilityRates

HttpFn = Callable[[urllib.request.Request, float], Tuple[int, bytes]]


class ZaiWalletError(Exception):
    """Wallet reconciliation failure."""


def _http_text(
    req: urllib.request.Request,
    http_fn: HttpFn,
    timeout: float = 25.0,
) -> Tuple[int, str]:
    status, body = http_fn(req, timeout)
    if isinstance(body, bytes):
        return status, body.decode(errors="replace")
    return status, body

LEGACY_ENV_WALLET_ID = "legacy-env"
ZAI_PROVIDER_SLUG = "zai"


@dataclass(frozen=True)
class ZaiWallet:
    entry_id: str
    runtime_api_key: str
    pool_label: str = ""


def wallet_reading_key(entry_id: str) -> str:
    return f"zai:{entry_id}"


def wallet_display_label(ordinal: int) -> str:
    return f"z.ai {ordinal}"


def _read_env_key(path, key: str) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _runtime_key_from_pool_entry(entry: Mapping[str, Any]) -> str:
    token = entry.get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    agent_key = entry.get("agent_key")
    if isinstance(agent_key, str) and agent_key.strip():
        return agent_key.strip()
    return ""


def read_zai_pool_raw(hermes_home) -> Tuple[Optional[List[Mapping[str, Any]]], bool]:
    """Return (pool_entries, pool_unreadable).

    ``pool_entries`` is None when unreadable. An empty list means a readable
    pool with an explicit empty ``credential_pool.zai`` list. Any non-mapping
    row in a non-empty list marks the snapshot unreadable for destructive
    reconcile while still returning valid mapping rows for enumeration.
    """
    auth_path = hermes_home / "auth.json"
    if not auth_path.exists():
        return None, True
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, True
    if not isinstance(raw, dict):
        return None, True
    pool = raw.get("credential_pool")
    if pool is None:
        return None, True
    if not isinstance(pool, dict):
        return None, True
    entries = pool.get("zai")
    if entries is None:
        return None, True
    if not isinstance(entries, list):
        return None, True
    if not entries:
        return [], False
    cleaned: List[Mapping[str, Any]] = []
    has_malformed = False
    for entry in entries:
        if isinstance(entry, Mapping):
            cleaned.append(entry)
        else:
            has_malformed = True
    if not cleaned:
        return None, True
    return cleaned, has_malformed


def enumerate_zai_wallets(hermes_home) -> Tuple[List[ZaiWallet], bool]:
    """Enumerate unique Z.AI wallets in stable pool order.

    First-seen entry id wins when two pool rows share the exact same runtime
    key (dedupe only; not malformed). Mapping rows missing id or runtime key
    mark the snapshot unreadable for destructive reconcile. When the readable
    pool is empty, a single ``secrets/zai.env`` key becomes a synthetic
    ``legacy-env`` wallet.
    """
    pool_entries, pool_unreadable = read_zai_pool_raw(hermes_home)

    wallets: List[ZaiWallet] = []
    seen_keys: Set[str] = set()
    if pool_entries is not None:
        for entry in pool_entries:
            entry_id = str(entry.get("id") or "").strip()
            if not entry_id:
                pool_unreadable = True
                continue
            runtime_key = _runtime_key_from_pool_entry(entry)
            if not runtime_key:
                pool_unreadable = True
                continue
            if runtime_key in seen_keys:
                continue
            seen_keys.add(runtime_key)
            label = str(entry.get("label") or "").strip()
            wallets.append(
                ZaiWallet(
                    entry_id=entry_id,
                    runtime_api_key=runtime_key,
                    pool_label=label,
                )
            )

    if (
        not pool_unreadable
        and pool_entries
        and not wallets
    ):
        pool_unreadable = True

    if not wallets:
        env_path = hermes_home / "secrets" / "zai.env"
        env_key = _read_env_key(env_path, "ZAI_API_KEY")
        if env_key:
            wallets.append(
                ZaiWallet(
                    entry_id=LEGACY_ENV_WALLET_ID,
                    runtime_api_key=env_key,
                    pool_label="legacy-env",
                )
            )
    return wallets, pool_unreadable


def assign_wallet_ordinals(
    wallets: Sequence[ZaiWallet],
    state: Mapping[str, Any],
) -> Tuple[Dict[str, int], int]:
    """Bind stable display numbers; never reclaim removed ordinals.

    Persists current entry ids plus high-water; retired numbers are not reused.
    """
    existing = dict(state.get("zai_wallet_ordinals") or {})
    high_water = int(state.get("zai_wallet_ordinal_high_water") or 0)
    if not high_water and existing:
        high_water = max(existing.values(), default=0)
    ordinals: Dict[str, int] = {}
    for wallet in wallets:
        entry_id = wallet.entry_id
        if entry_id in existing:
            ordinal = int(existing[entry_id])
        else:
            high_water += 1
            ordinal = high_water
            existing[entry_id] = ordinal
        ordinals[entry_id] = ordinal
    high_water = max(high_water, max(ordinals.values(), default=0))
    return ordinals, high_water


def _channel_exists(
    channel_id: str,
    headers: dict,
    http_fn: HttpFn,
) -> bool:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}",
        headers=headers,
    )
    status, text = _http_text(req, http_fn=http_fn)
    if status == 200:
        return True
    if status == 404:
        return False
    raise ZaiWalletError(
        f"discord channel probe returned {status}: {text[:200]}"
    )


def create_voice_channel(
    guild_id: str,
    category_id: str,
    headers: dict,
    http_fn: HttpFn,
    *,
    placeholder_name: str = "zai-wallet",
) -> str:
    body = {
        "name": placeholder_name,
        "type": 2,
        "parent_id": category_id,
    }
    req = urllib.request.Request(
        f"https://discord.com/api/v10/guilds/{guild_id}/channels",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    status, text = _http_text(req, http_fn=http_fn)
    if status not in (200, 201):
        raise ZaiWalletError(
            f"discord channel create returned {status}: {text[:200]}"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ZaiWalletError("discord: invalid channel create JSON") from exc
    channel_id = payload.get("id")
    if not channel_id:
        raise ZaiWalletError("discord: channel create response missing id")
    return str(channel_id)


def delete_voice_channel(
    channel_id: str,
    headers: dict,
    http_fn: HttpFn,
) -> None:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}",
        headers=headers,
        method="DELETE",
    )
    status, text = _http_text(req, http_fn=http_fn)
    if status not in (200, 204, 404):
        raise ZaiWalletError(
            f"discord channel delete returned {status}: {text[:200]}"
        )


WalletPersistFn = Callable[[Dict[str, str], Dict[str, int], int], None]


def _legacy_channel_unowned(
    legacy_channel: str,
    channels: Mapping[str, str],
) -> bool:
    if not legacy_channel:
        return False
    return legacy_channel not in set(channels.values())


def reconcile_zai_wallet_channels(
    config: Mapping[str, Any],
    wallets: Sequence[ZaiWallet],
    state: Mapping[str, Any],
    *,
    pool_unreadable: bool,
    headers: dict,
    http_fn: HttpFn,
    persist: Optional[WalletPersistFn] = None,
) -> Tuple[Dict[str, str], Dict[str, int], int, List[str]]:
    """Ensure each wallet has a live Discord voice channel.

    Returns (entry_id -> channel_id, ordinals, high_water, deleted_channel_ids).
    """
    channels = dict(state.get("zai_wallet_channels") or {})
    ordinals = dict(state.get("zai_wallet_ordinals") or {})
    high_water = int(state.get("zai_wallet_ordinal_high_water") or 0)
    if not high_water and ordinals:
        high_water = max(ordinals.values(), default=0)

    if pool_unreadable and not any(wallet.runtime_api_key for wallet in wallets):
        return channels, ordinals, high_water, []

    allow_deletes = not pool_unreadable
    ordinals, high_water = assign_wallet_ordinals(wallets, state)
    legacy_channel = str(config.get("channel_ids", {}).get("zai") or "")
    guild_id = str(config["guild_id"])
    category_id = str(config["category_id"])
    current_ids = {wallet.entry_id for wallet in wallets}
    deleted: List[str] = []

    def _persist() -> None:
        if persist is not None:
            persist(channels, ordinals, high_water)

    if wallets and legacy_channel and _legacy_channel_unowned(legacy_channel, channels):
        first_id = wallets[0].entry_id
        if first_id not in channels:
            channels[first_id] = legacy_channel
            _persist()

    for wallet in wallets:
        entry_id = wallet.entry_id
        channel_id = channels.get(entry_id)
        if channel_id:
            if not _channel_exists(channel_id, headers, http_fn):
                channel_id = None
        if not channel_id:
            channels[entry_id] = create_voice_channel(
                guild_id,
                category_id,
                headers,
                http_fn,
            )
            _persist()
        else:
            channels[entry_id] = channel_id

    if allow_deletes:
        for entry_id, channel_id in list(channels.items()):
            if entry_id not in current_ids:
                delete_voice_channel(channel_id, headers, http_fn)
                deleted.append(channel_id)
                del channels[entry_id]
                _persist()

    _persist()
    return channels, ordinals, high_water, deleted


def pick_best_zai_reading(
    wallet_readings: Mapping[str, Mapping[str, Any]],
    reliability: Optional[Mapping[str, ReliabilityRates]] = None,
) -> Optional[Dict[str, Any]]:
    """Legacy ``readings.zai`` alias = best currently spendable wallet."""
    if not wallet_readings:
        return None
    rates = reliability or {}
    best_entry: Optional[Dict[str, Any]] = None
    best_bucket = 2
    best_score = -1.0

    for entry in wallet_readings.values():
        reading = QuotaReading(
            channel_key="zai",
            provider=ZAI_PROVIDER_SLUG,
            channel_name="",
            pct=int(entry["pct"]),
            reset_seconds=float(entry["reset_seconds"]),
            reset_count=int(entry.get("reset_count") or 0),
            reset_expiry_seconds=(
                None
                if entry.get("reset_expiry_seconds") is None
                else float(entry["reset_expiry_seconds"])
            ),
            reset_expiry_horizons=tuple(entry.get("reset_expiry_horizons") or ()),
        )
        bucket = 1 if is_low_quota(reading) else 0
        score = score_provider(reading, rates.get(ZAI_PROVIDER_SLUG))
        if bucket < best_bucket or (bucket == best_bucket and score > best_score):
            best_bucket = bucket
            best_score = score
            best_entry = dict(entry)
    return best_entry


def wallets_from_state_when_unreadable(
    state: Mapping[str, Any],
    hermes_home,
) -> List[ZaiWallet]:
    """Reconstruct wallet identity from durable state when the pool is unreadable."""
    ordinals = state.get("zai_wallet_ordinals") or {}
    if not isinstance(ordinals, Mapping) or not ordinals:
        return []
    # keys are unavailable — callers must not fetch quota without a readable pool
    return [
        ZaiWallet(entry_id=str(entry_id), runtime_api_key="", pool_label="")
        for entry_id in ordinals
    ]


def redact_wallet_error(exc: BaseException, wallet: ZaiWallet) -> str:
    from plugins.quota_channels.core import _error_text, redact_secrets

    return redact_secrets(_error_text(exc), (wallet.runtime_api_key,))
