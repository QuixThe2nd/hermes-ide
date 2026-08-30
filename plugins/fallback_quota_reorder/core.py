"""Core logic — Discord channel reads, name parsing, fallback reorder, config writes."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from plugins.fallback_quota_reorder.reliability import (
    ReliabilityRates,
    rates_for_providers,
)

HttpFn = Callable[[urllib.request.Request, float], Tuple[int, bytes]]
NowFn = Callable[[], float]

CHANNEL_KEYS: Tuple[str, ...] = ("codex", "kimi", "zai", "grok", "cursor")

CHANNEL_KEY_TO_PROVIDER: Dict[str, str] = {
    "codex": "openai-codex",
    "kimi": "kimi-coding",
    "zai": "zai",
    "grok": "xai-oauth",
    "cursor": "cursor",
}

# Providers with a manual usage-limit resets API — the only ones whose
# pending resets earn the additive score term. Reset fields that leak into
# any other provider's channel name, state row, or reading are inert.
RESET_CREDIT_PROVIDERS = frozenset({"openai-codex", "xai-oauth"})
# the quota channel keys whose provider is in RESET_CREDIT_PROVIDERS
RESET_CREDIT_CHANNEL_KEYS = frozenset(
    key
    for key, slug in CHANNEL_KEY_TO_PROVIDER.items()
    if slug in RESET_CREDIT_PROVIDERS
)

DEFAULT_QUOTA_INTERVAL_SECONDS = 1800
POST_QUOTA_OFFSET_SECONDS = 120
STALE_MAX_AGE_SECONDS = 6 * 3600
STATE_FILENAME = "fallback_quota_reorder_state.json"
BACKUP_SUBDIR = Path("config-backups") / "fallback_quota_reorder"
MAX_BACKUPS = 20
LOW_QUOTA_PCT = 5
MIN_HOURS_REMAINING = 1.0 / 60.0  # 1 minute; zero hours would divide by zero
REFERENCE_HOURS = 168.0  # one week — the score's neutral time horizon

# Ox Alpha (openrouter/stealth/ox-alpha) is free/unlimited: treat it like a
# fresh account with zero usage — 100% synthetic quota against the full
# REFERENCE_HOURS window, so neutral uptime scores exactly 1.0 and observed
# uptime derates it through the same factors as everyone else. Only this
# exact route gets the treatment; every other openrouter model is an
# ordinary unscored entry without a real reading.
UNLIMITED_PROVIDER = "openrouter"
UNLIMITED_MODEL = "stealth/ox-alpha"
UNLIMITED_PCT = 100
UNLIMITED_RESET_SECONDS = 7 * 86400  # 604800s = 168h = REFERENCE_HOURS

DISCORD_USER_AGENT = "Hermes Agent (https://hermes-agent.nousresearch.com)"

_COUNTDOWN_GROUP = r"(\d+)(d|h|m) left"
_TOK_SEGMENT = r"(?: • \d+(?:\.\d+)?[KMB]? tok/7d)?"
# trailing pending-reset segment from quota_channels, e.g. " • 2 resets" or
# " • 1 reset in 2d" (the countdown is only rendered when the count is nonzero)
_RESETS_SEGMENT = r"(?: • (\d+) resets?(?: in (\d+)(d|h|m))?)?"

STANDARD_NAME_RE = re.compile(
    rf"^(Codex|Kimi|Grok|z\.ai): (\d+)%{_TOK_SEGMENT} • {_COUNTDOWN_GROUP}"
    rf"{_RESETS_SEGMENT}$"
)
CURSOR_NAME_RE = re.compile(
    rf"^Cursor: (\d+)%/(\d+)%{_TOK_SEGMENT} • {_COUNTDOWN_GROUP}$"
)


class FallbackQuotaReorderError(Exception):
    """Raised instead of sys.exit from the CLI entrypoint."""


@dataclass(frozen=True)
class QuotaReading:
    channel_key: str
    provider: str
    channel_name: str
    pct: int
    reset_seconds: float
    # pending manual usage-limit resets (Codex/Grok only). ``reset_count`` of
    # 0 means no extra wallets; ``reset_expiry_seconds`` of None means the
    # expiry is unknown, so the credit stays counted but scores nothing —
    # the usage-reset countdown is never borrowed as a stand-in.
    reset_count: int = 0
    reset_expiry_seconds: Optional[float] = None


@dataclass(frozen=True)
class PrimarySlot:
    """The active model pair: ``model.provider`` + ``model.default``."""

    provider: str
    model: str


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def state_path() -> Path:
    return _hermes_home() / STATE_FILENAME


def backup_dir() -> Path:
    return _hermes_home() / BACKUP_SUBDIR


def _read_env_key(path: Path, key: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError as exc:
        raise FallbackQuotaReorderError(f"cannot read {path}: {exc}") from exc
    raise FallbackQuotaReorderError(f"{key} missing in {path}")


def discord_token() -> str:
    return _read_env_key(_hermes_home() / ".env", "DISCORD_BOT_TOKEN")


def discord_headers() -> dict:
    return {
        "Authorization": "Bot " + discord_token(),
        "User-Agent": DISCORD_USER_AGENT,
    }


def default_http(req: urllib.request.Request, timeout: float = 25.0) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise FallbackQuotaReorderError(
            f"network error: {type(exc).__name__}: {exc}"
        ) from exc


def http_text(
    req: urllib.request.Request,
    http_fn: HttpFn = default_http,
    timeout: float = 25.0,
) -> Tuple[int, str]:
    status, body = http_fn(req, timeout)
    if isinstance(body, bytes):
        return status, body.decode(errors="replace")
    return status, body


def parse_countdown_seconds(value: int, unit: str) -> int:
    multipliers = {"d": 86400, "h": 3600, "m": 60}
    if unit not in multipliers:
        raise FallbackQuotaReorderError(f"invalid countdown unit: {unit!r}")
    return value * multipliers[unit]


def parse_channel_name(channel_key: str, channel_name: str) -> Optional[QuotaReading]:
    """Return a reading when the name matches strictly; None when unreadable."""
    if channel_key not in CHANNEL_KEY_TO_PROVIDER:
        return None
    name = channel_name or ""
    provider = CHANNEL_KEY_TO_PROVIDER[channel_key]

    match = CURSOR_NAME_RE.match(name) if channel_key == "cursor" else STANDARD_NAME_RE.match(
        name
    )
    if not match:
        return None

    if channel_key == "cursor":
        pct_a, pct_b, reset_value, reset_unit = (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4),
        )
        pct = min(pct_a, pct_b)
        reset_count = 0
        reset_expiry_seconds = None
    else:
        pct = int(match.group(2))
        reset_value = int(match.group(3))
        reset_unit = match.group(4)
        if channel_key in RESET_CREDIT_CHANNEL_KEYS:
            reset_count = int(match.group(5) or 0)
            reset_expiry_seconds = (
                None
                if match.group(6) is None
                else parse_countdown_seconds(int(match.group(6)), match.group(7))
            )
        else:
            # Kimi/z.ai have no resets API: a resets segment that polluted
            # their name parses, but never reaches the reading
            reset_count = 0
            reset_expiry_seconds = None

    reset_seconds = parse_countdown_seconds(reset_value, reset_unit)
    return QuotaReading(
        channel_key=channel_key,
        provider=provider,
        channel_name=name,
        pct=pct,
        reset_seconds=reset_seconds,
        reset_count=reset_count,
        reset_expiry_seconds=reset_expiry_seconds,
    )


def is_unlimited_route(provider: object, model: object) -> bool:
    """True only for the exact ``openrouter/stealth/ox-alpha`` route.

    Provider and model are case-normalized the same way entry identities
    are (strip + lower), so ``OpenRouter/Stealth/OX-Alpha`` also matches —
    but no other openrouter model ever does.
    """
    return (
        str(provider or "").strip().lower() == UNLIMITED_PROVIDER
        and str(model or "").strip().lower() == UNLIMITED_MODEL
    )


def unlimited_reading() -> QuotaReading:
    """Synthetic full-wallet reading for the unlimited Ox Alpha route.

    100% quota against exactly REFERENCE_HOURS, so with no reliability
    samples the route scores exactly 1.0 and observed uptime is the only
    thing that moves it.
    """

    return QuotaReading(
        channel_key="",
        provider=UNLIMITED_PROVIDER,
        channel_name="",
        pct=UNLIMITED_PCT,
        reset_seconds=UNLIMITED_RESET_SECONDS,
    )


def reading_for_entry(
    entry: Mapping[str, Any],
    readings_by_provider: Mapping[str, QuotaReading],
) -> Optional[QuotaReading]:
    """The reading that scores ``entry``: real channel data, else synthetic.

    Real per-provider readings win; the synthetic unlimited reading only
    fills the exact Ox Alpha route, which has no quota channel of its own.
    """
    provider = str(entry.get("provider") or "").strip().lower()
    reading = readings_by_provider.get(provider)
    if reading is not None:
        return reading
    if is_unlimited_route(provider, entry.get("model")):
        return unlimited_reading()
    return None


def load_state() -> dict:
    path = state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".fallback-quota-reorder.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        raise FallbackQuotaReorderError(f"cannot write {path}: {exc}") from exc


def load_config_section(config_path: Optional[Path] = None) -> dict:
    if config_path is None:
        try:
            from hermes_cli.config import load_config_readonly

            raw = load_config_readonly()
        except Exception as exc:
            raise FallbackQuotaReorderError(f"cannot load config: {exc}") from exc
    else:
        import yaml

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except OSError as exc:
            raise FallbackQuotaReorderError(f"cannot read {config_path}: {exc}") from exc
        except Exception as exc:
            raise FallbackQuotaReorderError(f"cannot parse {config_path}: {exc}") from exc
    return raw if isinstance(raw, dict) else {}


def validate_channel_config(raw: Mapping[str, Any]) -> Tuple[Dict[str, str], int]:
    section = raw.get("quota_channels")
    if not isinstance(section, Mapping):
        raise FallbackQuotaReorderError("quota_channels section missing in config.yaml")

    channel_ids = section.get("channel_ids") or {}
    if not isinstance(channel_ids, Mapping):
        raise FallbackQuotaReorderError("quota_channels.channel_ids must be a mapping")

    resolved: Dict[str, str] = {}
    for key in CHANNEL_KEYS:
        channel_id = channel_ids.get(key)
        if not channel_id:
            raise FallbackQuotaReorderError(
                f"quota_channels.channel_ids.{key} required for fallback quota reorder"
            )
        resolved[key] = str(channel_id)

    interval = int(section.get("quota_interval_seconds", DEFAULT_QUOTA_INTERVAL_SECONDS))
    return resolved, interval


def fetch_channel_name(
    channel_id: str,
    headers: dict,
    http_fn: HttpFn = default_http,
) -> str:
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}", headers=headers
    )
    status, text = http_text(req, http_fn=http_fn)
    if status != 200:
        raise FallbackQuotaReorderError(
            f"discord channel fetch returned {status}: {text[:200]}"
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FallbackQuotaReorderError("discord: invalid channel response JSON") from exc
    name = data.get("name")
    if not isinstance(name, str):
        raise FallbackQuotaReorderError("discord: channel response missing name")
    return name


def fetch_channel_names(
    channel_ids: Mapping[str, str],
    http_fn: HttpFn = default_http,
) -> Dict[str, str]:
    headers = discord_headers()
    names: Dict[str, str] = {}
    for key in CHANNEL_KEYS:
        names[key] = fetch_channel_name(channel_ids[key], headers, http_fn=http_fn)
    return names


def readings_from_names(
    names: Mapping[str, str],
    precise_readings: Optional[Mapping[str, Tuple[int, float]]] = None,
    precise_reset_fields: Optional[Mapping[str, Tuple[int, Optional[float]]]] = None,
) -> Dict[str, QuotaReading]:
    readings: Dict[str, QuotaReading] = {}
    for key in CHANNEL_KEYS:
        name = names.get(key, "")
        reading = parse_channel_name(key, name)
        precise = (precise_readings or {}).get(key)
        if precise is not None:
            # precise state beats the day-rounded channel name; it also scores
            # providers whose name did not parse strictly
            pct, reset_seconds = precise
            base = reading or QuotaReading(
                channel_key=key,
                provider=CHANNEL_KEY_TO_PROVIDER[key],
                channel_name=name,
                pct=pct,
                reset_seconds=reset_seconds,
            )
            overrides: Dict[str, Any] = {"pct": pct, "reset_seconds": reset_seconds}
            resets = (precise_reset_fields or {}).get(key)
            if resets is not None:
                # state that carries reset credits replaces the name-parsed
                # ones; state that predates them keeps what the name showed
                overrides["reset_count"] = resets[0]
                overrides["reset_expiry_seconds"] = resets[1]
            reading = replace(base, **overrides)
        if reading is not None:
            readings[reading.provider] = reading
    return readings


def _fresh_quota_state_readings(
    quota_interval_seconds: int,
    *,
    now_fn: NowFn = time.time,
) -> Mapping[str, Any]:
    """quota_channels state readings mapping, or {} when unusable.

    The one freshness gate shared by ``load_precise_readings`` and
    ``load_precise_reset_fields``: empty when the state file is
    missing/corrupt, predates the readings schema, or its
    last_quota_success is older than 2 * quota_interval_seconds — callers
    then fall back to strict channel-name parsing.
    """
    from plugins.quota_channels.core import state_path as quota_state_path

    try:
        raw = json.loads(quota_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    try:
        last_success = float(raw.get("last_quota_success") or 0)
    except (TypeError, ValueError):
        return {}
    if last_success <= 0:
        return {}
    if now_fn() - last_success > 2 * quota_interval_seconds:
        return {}
    entries = raw.get("readings")
    if not isinstance(entries, Mapping):
        return {}
    return entries


def load_precise_readings(
    quota_interval_seconds: int,
    *,
    now_fn: NowFn = time.time,
) -> Dict[str, Tuple[int, float]]:
    """Map provider slug -> (pct, reset_seconds) from quota_channels state.

    Empty when the state file is missing/corrupt, predates the readings
    schema, or its last_quota_success is older than 2 * quota_interval_seconds
    — callers then fall back to strict channel-name parsing.
    """
    entries = _fresh_quota_state_readings(quota_interval_seconds, now_fn=now_fn)
    precise: Dict[str, Tuple[int, float]] = {}
    for slug, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        try:
            pct = int(entry["pct"])
            reset_seconds = float(entry["reset_seconds"])
        except (KeyError, TypeError, ValueError):
            continue
        precise[str(slug)] = (pct, reset_seconds)
    return precise


def load_precise_reset_fields(
    quota_interval_seconds: int,
    *,
    now_fn: NowFn = time.time,
) -> Dict[str, Tuple[int, Optional[float]]]:
    """Map slug -> (reset_count, reset_expiry_seconds_or_None) from state.

    Same state file and freshness rules as ``load_precise_readings``. Only
    rows whose provider has a resets API (see ``RESET_CREDIT_PROVIDERS``)
    and that actually carry a ``reset_count`` appear — everyone else is
    absent, so callers keep whatever the channel name parsed instead of an
    invented zero. An unreadable count drops the row the same way; a
    missing or unreadable expiry stays None (no separate clock).
    """
    entries = _fresh_quota_state_readings(quota_interval_seconds, now_fn=now_fn)
    fields: Dict[str, Tuple[int, Optional[float]]] = {}
    for slug, entry in entries.items():
        if not isinstance(entry, Mapping):
            continue
        provider = str(CHANNEL_KEY_TO_PROVIDER.get(slug, slug)).strip().lower()
        if provider not in RESET_CREDIT_PROVIDERS:
            continue
        if "reset_count" not in entry:
            continue
        try:
            count = int(entry["reset_count"])
        except (TypeError, ValueError):
            continue
        expiry: Optional[float] = None
        if entry.get("reset_expiry_seconds") is not None:
            try:
                expiry = float(entry["reset_expiry_seconds"])
            except (TypeError, ValueError):
                expiry = None
        fields[str(slug)] = (count, expiry)
    return fields


def _entry_identity(entry: Mapping[str, Any]) -> Tuple[str, str, str]:
    from hermes_cli.fallback_config import _entry_identity as fallback_identity

    return fallback_identity(dict(entry))


def order_signature(entries: Sequence[Mapping[str, Any]]) -> Tuple[Tuple[str, str, str], ...]:
    return tuple(_entry_identity(entry) for entry in entries)


def chain_signature(config: Mapping[str, Any]) -> Tuple[Tuple[str, str, str], ...]:
    from hermes_cli.fallback_config import get_fallback_chain

    return order_signature(get_fallback_chain(dict(config)))


def format_entry_label(entry: Mapping[str, Any]) -> str:
    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or entry.get("default") or "").strip()
    base_url = str(entry.get("base_url") or "").strip()
    if base_url:
        return f"{provider}/{model}@{base_url}"
    return f"{provider}/{model}"


def format_readings_line(
    readings: Mapping[str, QuotaReading],
    reliability: Optional[Mapping[str, ReliabilityRates]] = None,
    scores: Optional[Mapping[str, float]] = None,
) -> str:
    parts: List[str] = []
    for key in CHANNEL_KEYS:
        provider = CHANNEL_KEY_TO_PROVIDER[key]
        reading = readings.get(provider)
        if reading is None:
            parts.append(f"{key}=unreadable")
            continue
        chunk = f"{key}={reading.pct}%@{reading.reset_seconds}s ({provider})"
        rates = (reliability or {}).get(provider)
        if rates is not None:
            chunk += (
                f" up24={rates.rate_24h:.0%}/{rates.samples_24h}"
                f" up1h={rates.rate_1h:.0%}/{rates.samples_1h}"
            )
        if scores is not None and provider in scores:
            chunk += f" score={scores[provider]:.2f}"
        parts.append(chunk)
    return ", ".join(parts)


def _reset_credit_count(reading: QuotaReading) -> int:
    """The pending-reset count that actually scores for ``reading``.

    Contract gate: only providers with a resets API
    (``RESET_CREDIT_PROVIDERS`` — Codex/Grok) have reset credits, so a
    reset field injected into any other provider's reading counts as zero
    everywhere: no score term, no low-quota escape.
    """
    if str(reading.provider).strip().lower() not in RESET_CREDIT_PROVIDERS:
        return 0
    return max(0, int(reading.reset_count))


def score_provider(
    reading: QuotaReading,
    rates: Optional[ReliabilityRates] = None,
) -> float:
    """Higher is better: spend the soonest-reset wallets first, derate flaky ones.

    score = (remaining_term + reset_term) * rate_24h * rate_1h
    remaining_term = quota_frac * (REFERENCE_HOURS / hours_remaining)
    reset_term     = reset_count * (REFERENCE_HOURS / hours_reset_expires)

    Time enters inversely: the sooner a wallet refills, the more urgent it
    is to burn it now, while a wallet resetting in exactly REFERENCE_HOURS
    (or later) scores its quota fraction 1:1 or less. Unknown reliability
    stays 1.0 so a quiet provider is not punished. Hours remaining floor at
    one minute so a nearly-reset wallet is not divided by zero.

    Each pending manual usage-limit reset adds its own full wallet
    (quota fraction 1.0) on the reset-expiry clock — additive and stackable,
    with no cap. The invariant: one pending reset at 0% remaining scores
    exactly like zero resets at 100% remaining when the two clocks match.
    A reset whose expiry is unknown adds nothing: urgency is not measurable,
    so the usage-reset countdown is never borrowed as a stand-in (the credit
    stays visible as a count and still lifts the wallet out of the low-quota
    sink). A count of 0 adds nothing either. Only Codex/Grok have a resets
    API at all — reset fields on any other provider are inert (see
    ``_reset_credit_count``).
    """
    hours = max(float(reading.reset_seconds) / 3600.0, MIN_HOURS_REMAINING)
    quota_frac = max(0.0, min(float(reading.pct) / 100.0, 1.0))
    resolved = rates or ReliabilityRates()
    remaining_term = quota_frac * (REFERENCE_HOURS / hours)
    reset_count = _reset_credit_count(reading)
    reset_term = 0.0
    if reset_count and reading.reset_expiry_seconds is not None:
        reset_hours = max(
            float(reading.reset_expiry_seconds) / 3600.0, MIN_HOURS_REMAINING
        )
        reset_term = reset_count * (REFERENCE_HOURS / reset_hours)
    return (remaining_term + reset_term) * resolved.rate_24h * resolved.rate_1h


def is_low_quota(reading: QuotaReading) -> bool:
    """True when the reading sinks into the low-quota bucket.

    A 0% wallet with at least one pending usage-limit reset still holds
    spendable capacity, so it stays in the healthy bucket (even when that
    reset's expiry is unknown and it therefore scores nothing); only a
    genuinely empty wallet with zero pending resets sinks. Resets only exist
    for Codex/Grok — an injected count on any other provider sinks with the
    wallet it polluted.
    """
    return reading.pct < LOW_QUOTA_PCT and _reset_credit_count(reading) <= 0


def compute_desired_order(
    entries: Sequence[Mapping[str, Any]],
    readings_by_provider: Mapping[str, QuotaReading],
    reliability: Optional[Mapping[str, ReliabilityRates]] = None,
) -> List[dict]:
    if not entries:
        return []

    rates = reliability or {}
    indexed = list(enumerate(entries))
    scored_healthy: List[Tuple[int, dict, float]] = []
    scored_low: List[Tuple[int, dict, float]] = []
    unscored: List[Tuple[int, dict]] = []

    for index, entry in indexed:
        reading = reading_for_entry(entry, readings_by_provider)
        if reading is None:
            unscored.append((index, dict(entry)))
            continue

        provider = str(entry.get("provider") or "").strip().lower()
        item = (index, dict(entry), score_provider(reading, rates.get(provider)))
        if is_low_quota(reading):
            scored_low.append(item)
        else:
            scored_healthy.append(item)

    # highest score first; original index breaks exact ties
    scored_healthy.sort(key=lambda item: (-item[2], item[0]))
    scored_low.sort(key=lambda item: (-item[2], item[0]))

    ordered: List[dict] = [entry for _, entry, _ in scored_healthy]
    ordered.extend(entry for _, entry, _ in scored_low)
    ordered.extend(entry for _, entry in unscored)
    return ordered


def current_primary(config: Mapping[str, Any]) -> Optional[PrimarySlot]:
    """The active primary as model.default + model.provider, when both are set."""
    model_section = config.get("model")
    if not isinstance(model_section, Mapping):
        return None
    provider = str(model_section.get("provider") or "").strip()
    model = str(model_section.get("default") or model_section.get("model") or "").strip()
    if not provider or not model:
        return None
    return PrimarySlot(provider=provider, model=model)


def _unlimited_chain_entry(
    entries: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    for entry in entries:
        if is_unlimited_route(entry.get("provider"), entry.get("model")):
            return entry
    return None


def compute_primary_slot(
    config: Mapping[str, Any],
    desired_entries: Sequence[Mapping[str, Any]],
    readings: Mapping[str, QuotaReading],
    reliability: Optional[Mapping[str, ReliabilityRates]] = None,
) -> Optional[PrimarySlot]:
    """Pick the primary winner among scored candidates with a chain entry.

    Candidates are the tracked providers with a reading plus the unlimited
    ``openrouter/stealth/ox-alpha`` route, all scored with the same
    ``score_provider`` math — Ox Alpha through its synthetic full-quota
    reading. Only candidates that have a desired_entries entry with a
    non-empty model string can be promoted — the model string has to come
    from somewhere. A top scorer without a chain entry is skipped, and the
    best scorer WITH one wins instead. The ``compute_desired_order``
    low-quota bucket applies to this race too: a reading ``is_low_quota``
    never beats a healthy candidate at any raw score and never displaces a
    healthy current primary — it only wins a race where every candidate and
    the primary are sunk, by raw score among themselves. The current
    primary is scored too (an untracked primary such as a plain openrouter
    route scores 0) and wins ties. Ties between eligible tracked providers
    resolve to the lowest CHANNEL_KEYS index; the unlimited route competes
    after them, so it only wins by beating the best tracked score outright.
    Returns None — "leave the primary alone" — when there is no usable
    current primary, no eligible candidate at all, or none beats the
    current primary's score.
    """
    current = current_primary(config)
    if current is None:
        return None

    entry_by_provider: Dict[str, Mapping[str, Any]] = {}
    for entry in desired_entries:
        provider = str(entry.get("provider") or "").strip().lower()
        if provider and provider not in entry_by_provider:
            entry_by_provider[provider] = entry

    rates = reliability or {}
    current_slug = current.provider.lower()
    current_reading = readings.get(current_slug)
    if current_reading is not None:
        current_score: Optional[float] = score_provider(
            current_reading, rates.get(current_slug)
        )
    elif is_unlimited_route(current.provider, current.model):
        current_score = score_provider(
            unlimited_reading(), rates.get(current_slug)
        )
    else:
        current_score = None

    # (bucket, score, entry): bucket 1 is the low-quota sink, so a healthy
    # candidate beats a sunk one at any raw score — a sub-5% wallet with no
    # pending reset can carry the highest number in the race (its
    # hours-remaining denominator floors at one minute) yet sink behind
    # every healthy entry in the chain; the primary race must sink with it.
    best: Optional[Tuple[int, float, Mapping[str, Any]]] = None

    def _consider(reading: QuotaReading, entry: Mapping[str, Any], score: float) -> None:
        nonlocal best
        bucket = 1 if is_low_quota(reading) else 0
        if best is None or (bucket, -score) < (best[0], -best[1]):
            best = (bucket, score, entry)

    for key in CHANNEL_KEYS:  # CHANNEL_KEYS order breaks score ties
        provider = CHANNEL_KEY_TO_PROVIDER[key]
        reading = readings.get(provider)
        if reading is None:
            continue
        entry = entry_by_provider.get(provider)
        if entry is None or not str(entry.get("model") or "").strip():
            continue
        _consider(reading, entry, score_provider(reading, rates.get(provider)))

    unlimited = _unlimited_chain_entry(desired_entries)
    if unlimited is not None:
        _consider(
            unlimited_reading(),
            unlimited,
            score_provider(unlimited_reading(), rates.get(UNLIMITED_PROVIDER)),
        )

    if best is None:
        return None
    # a sunk winner only ever takes the slot from another sunk primary, never
    # from a healthy one; an untracked primary is unscored, which the low
    # bucket still outranks — exactly the chain's bucket order
    current_healthy = (
        not is_low_quota(current_reading)
        if current_reading is not None
        else is_unlimited_route(current.provider, current.model)
    )
    if best[0] and current_healthy:
        return None
    if (current_score or 0.0) >= best[1]:
        return None

    winner = best[2]
    return PrimarySlot(
        provider=str(winner.get("provider") or "").strip(),
        model=str(winner.get("model") or "").strip(),
    )


def _chain_rank_key(
    entry: Mapping[str, Any],
    readings: Mapping[str, QuotaReading],
    rates: Mapping[str, ReliabilityRates],
) -> Tuple[int, float]:
    """Ordering key mirroring the compute_desired_order buckets.

    (0) healthy scored, (1) low-quota scored, (2) unscored; within the
    scored buckets higher score sorts earlier via ``-score``. The unlimited
    Ox Alpha route lands in the healthy bucket via its synthetic reading;
    any other openrouter model without a real reading is unscored.
    """
    reading = reading_for_entry(entry, readings)
    if reading is None:
        return (2, 0.0)
    provider = str(entry.get("provider") or "").strip().lower()
    score = score_provider(reading, rates.get(provider))
    return (1 if is_low_quota(reading) else 0, -score)


def rotate_chain_for_primary(
    desired_entries: Sequence[Mapping[str, Any]],
    promoted: PrimarySlot,
    displaced: PrimarySlot,
    readings: Mapping[str, QuotaReading],
    reliability: Optional[Mapping[str, ReliabilityRates]] = None,
) -> List[dict]:
    """Swap chain membership for a primary rotation.

    Drops the promoted provider's entry (it graduates to the primary slot)
    and splices an entry for the displaced previous primary in by the
    compute_desired_order buckets: healthy before low-quota before unscored,
    score descending within the scored buckets. A displaced Ox Alpha primary
    re-enters by its synthetic score; any other untracked previous primary
    (score 0) lands at the END of the chain.
    """
    rates = reliability or {}
    displaced_entry = {"provider": displaced.provider, "model": displaced.model}
    displaced_key = _chain_rank_key(displaced_entry, readings, rates)

    rotated: List[dict] = []
    dropped = False
    inserted = False
    for entry in desired_entries:
        provider = str(entry.get("provider") or "").strip().lower()
        if (
            not dropped
            and provider == promoted.provider.lower()
            and str(entry.get("model") or "").strip() == promoted.model
        ):
            dropped = True
            continue
        if not inserted and _chain_rank_key(entry, readings, rates) > displaced_key:
            rotated.append(displaced_entry)
            inserted = True
        rotated.append(dict(entry))
    if not inserted:
        rotated.append(displaced_entry)
    return rotated


def validate_fallback_entries(entries: Sequence[Mapping[str, Any]]) -> None:
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if "default" in entry:
            raise FallbackQuotaReorderError(
                'fallback_providers entry uses deprecated key "default"; use "model" instead'
            )


def names_byte_identical(
    current: Mapping[str, str], previous: Mapping[str, str]
) -> bool:
    for key in CHANNEL_KEYS:
        if current.get(key, "") != previous.get(key, ""):
            return False
    return True


def should_count_stale_tick(
    names: Mapping[str, str],
    readings: Mapping[str, QuotaReading],
    state: Mapping[str, Any],
    quota_interval_seconds: int,
    now_fn: NowFn = time.time,
) -> bool:
    previous_names = state.get("last_names") or {}
    if not isinstance(previous_names, Mapping):
        return False
    if not names_byte_identical(names, previous_names):
        return False

    try:
        last_ts = float(state.get("last_timestamp") or 0)
    except (TypeError, ValueError):
        last_ts = 0.0
    if last_ts <= 0:
        return False
    if now_fn() - last_ts > STALE_MAX_AGE_SECONDS:
        return False

    threshold = 2 * quota_interval_seconds
    return any(reading.reset_seconds <= threshold for reading in readings.values())


def update_staleness_state(
    names: Mapping[str, str],
    readings: Mapping[str, QuotaReading],
    state: dict,
    quota_interval_seconds: int,
    *,
    now_fn: NowFn = time.time,
) -> dict:
    new_state = dict(state)
    if should_count_stale_tick(names, readings, state, quota_interval_seconds, now_fn=now_fn):
        new_state["consecutive_stale"] = int(state.get("consecutive_stale") or 0) + 1
    else:
        new_state["consecutive_stale"] = 0
    new_state["last_names"] = {key: names.get(key, "") for key in CHANNEL_KEYS}
    new_state["last_timestamp"] = int(now_fn())
    return new_state


def is_frozen(state: Mapping[str, Any]) -> bool:
    return int(state.get("consecutive_stale") or 0) >= 2


def recommended_cron_spec(quota_interval_seconds: int = DEFAULT_QUOTA_INTERVAL_SECONDS) -> str:
    offset_min = max(1, round(POST_QUOTA_OFFSET_SECONDS / 60))
    period_min = max(1, quota_interval_seconds // 60)
    minutes: List[int] = []
    minute = offset_min
    while minute < 60:
        minutes.append(minute)
        minute += period_min
    return f"{','.join(str(value) for value in minutes)} * * * *"


def _prune_backups() -> None:
    directory = backup_dir()
    files = sorted(
        directory.glob("config-*.yaml"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in files[MAX_BACKUPS:]:
        try:
            stale.unlink()
        except OSError:
            pass


def backup_config() -> Path:
    from hermes_cli.config import get_config_path

    src = get_config_path()
    if not src.exists():
        raise FallbackQuotaReorderError(f"config file not found: {src}")

    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = directory / f"config-{stamp}.yaml"
    shutil.copy2(src, dst)
    _prune_backups()
    return dst


def restore_config(backup_path: Path) -> None:
    from hermes_cli.config import get_config_path, save_config
    import yaml

    try:
        restored = yaml.safe_load(backup_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise FallbackQuotaReorderError(
            f"cannot read backup {backup_path}: {exc}"
        ) from exc
    if not isinstance(restored, dict):
        raise FallbackQuotaReorderError(f"backup {backup_path} is not a mapping")
    save_config(restored, merge_existing=False)


def _primary_slot_matches(config: Mapping[str, Any], slot: PrimarySlot) -> bool:
    model_section = config.get("model")
    if not isinstance(model_section, Mapping):
        return False
    provider = str(model_section.get("provider") or "").strip().lower()
    model = str(model_section.get("default") or model_section.get("model") or "").strip()
    return provider == slot.provider.lower() and model == slot.model


def write_fallback_order(
    desired_entries: Sequence[Mapping[str, Any]],
    expected_signature: Tuple[Tuple[str, str, str], ...],
    primary_slot: Optional[PrimarySlot] = None,
) -> None:
    """Write the fallback chain (and optionally the primary slot) in one save.

    ``primary_slot`` swaps model.default/model.provider in the SAME
    save_config call as the chain reorder, so a failure can never leave a
    half-rotated config; post-write verification re-checks BOTH the chain
    signature and the primary keys, restoring the backup on any mismatch.
    """
    from hermes_cli.config import load_config, save_config

    config = load_config()
    validate_fallback_entries(config.get("fallback_providers") or [])
    validate_fallback_entries(desired_entries)

    backup_path = backup_config()
    config["fallback_providers"] = [dict(entry) for entry in desired_entries]
    if primary_slot is not None:
        model_section = config.get("model")
        if not isinstance(model_section, dict):
            model_section = {}
            config["model"] = model_section
        model_section["default"] = primary_slot.model
        model_section["provider"] = primary_slot.provider
    try:
        save_config(config)
        reloaded = load_config()
        chain = chain_signature(reloaded)
        if len(chain) <= 0:
            restore_config(backup_path)
            raise FallbackQuotaReorderError(
                "verification failed: fallback chain is empty after write"
            )
        if chain != expected_signature:
            restore_config(backup_path)
            raise FallbackQuotaReorderError(
                "verification failed: fallback chain order does not match desired order"
            )
        if primary_slot is not None and not _primary_slot_matches(reloaded, primary_slot):
            restore_config(backup_path)
            raise FallbackQuotaReorderError(
                "verification failed: primary model slot does not match desired "
                "provider/model"
            )
    except FallbackQuotaReorderError:
        raise
    except Exception as exc:
        restore_config(backup_path)
        raise FallbackQuotaReorderError(f"config write failed: {exc}") from exc


def run_reorder(
    *,
    config_path: Optional[Path] = None,
    force_quota: bool = False,
    dry_run: bool = False,
    http_fn: HttpFn = default_http,
    now_fn: NowFn = time.time,
) -> dict:
    raw = load_config_section(config_path)
    channel_ids, quota_interval_seconds = validate_channel_config(raw)

    names = fetch_channel_names(channel_ids, http_fn=http_fn)
    name_readings = readings_from_names(names)
    readings = readings_from_names(
        names,
        load_precise_readings(quota_interval_seconds, now_fn=now_fn),
        load_precise_reset_fields(quota_interval_seconds, now_fn=now_fn),
    )

    from hermes_cli.config import load_config

    config = load_config()
    current_entries = [
        dict(entry)
        for entry in (config.get("fallback_providers") or [])
        if isinstance(entry, dict)
    ]
    validate_fallback_entries(current_entries)

    primary_previous = current_primary(config)
    # Reliability is needed for every scored or current candidate of the
    # primary selection, not just the fallback entries: a promoted primary
    # (Ox Alpha included) is absent from fallback_providers, yet its rates
    # decide whether it keeps the slot.
    reliability_providers = {
        str(entry.get("provider") or "") for entry in current_entries
    }
    if primary_previous is not None:
        reliability_providers.add(primary_previous.provider)
    reliability_providers.update(readings.keys())
    reliability = rates_for_providers(reliability_providers, now_fn=now_fn)
    scores = {
        provider: score_provider(reading, reliability.get(provider))
        for provider, reading in readings.items()
    }
    desired_entries = compute_desired_order(
        current_entries, readings, reliability=reliability
    )
    primary_slot = compute_primary_slot(
        config, desired_entries, readings, reliability=reliability
    )
    if primary_slot is not None and primary_previous is not None:
        desired_entries = rotate_chain_for_primary(
            desired_entries,
            primary_slot,
            primary_previous,
            readings,
            reliability=reliability,
        )
    current_sig = chain_signature(config)
    desired_sig = chain_signature({**config, "fallback_providers": desired_entries})

    state = load_state()
    # staleness freeze stays channel-name based: byte-identical names plus
    # name-parsed reset thresholds, unaffected by precise state
    new_state = update_staleness_state(
        names, name_readings, state, quota_interval_seconds, now_fn=now_fn
    )
    frozen = is_frozen(new_state) and not force_quota
    primary_change = primary_slot is not None
    would_change = (desired_sig != current_sig or primary_change) and not frozen

    result = {
        "names": names,
        "readings": readings,
        "reliability": reliability,
        "scores": scores,
        "current_entries": current_entries,
        "desired_entries": desired_entries,
        "current_signature": current_sig,
        "desired_signature": desired_sig,
        "primary_current": primary_previous,
        "primary_desired": primary_slot,
        "would_change": would_change,
        "frozen": frozen,
        "consecutive_stale": int(new_state.get("consecutive_stale") or 0),
    }

    if dry_run:
        return result

    save_state(new_state)  # dry-run performs no state or config writes

    if frozen:
        return result

    if desired_sig == current_sig and not primary_change:
        return result

    write_fallback_order(desired_entries, desired_sig, primary_slot=primary_slot)
    return result
