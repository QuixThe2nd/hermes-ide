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

DEFAULT_QUOTA_INTERVAL_SECONDS = 1800
POST_QUOTA_OFFSET_SECONDS = 120
STALE_MAX_AGE_SECONDS = 6 * 3600
STATE_FILENAME = "fallback_quota_reorder_state.json"
BACKUP_SUBDIR = Path("config-backups") / "fallback_quota_reorder"
MAX_BACKUPS = 20
LOW_QUOTA_PCT = 5
MIN_HOURS_REMAINING = 1.0 / 60.0  # 1 minute; zero hours would zero the score

DISCORD_USER_AGENT = "Hermes Agent (https://hermes-agent.nousresearch.com)"

_COUNTDOWN_GROUP = r"(\d+)(d|h|m) left"
_TOK_SEGMENT = r"(?: • \d+(?:\.\d+)?[KMB]? tok/7d)?"

STANDARD_NAME_RE = re.compile(
    rf"^(Codex|Kimi|Grok|z\.ai): (\d+)%{_TOK_SEGMENT} • {_COUNTDOWN_GROUP}$"
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
    else:
        pct = int(match.group(2))
        reset_value = int(match.group(3))
        reset_unit = match.group(4)

    reset_seconds = parse_countdown_seconds(reset_value, reset_unit)
    return QuotaReading(
        channel_key=channel_key,
        provider=provider,
        channel_name=name,
        pct=pct,
        reset_seconds=reset_seconds,
    )


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
            reading = replace(base, pct=pct, reset_seconds=reset_seconds)
        if reading is not None:
            readings[reading.provider] = reading
    return readings


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


def score_provider(
    reading: QuotaReading,
    rates: Optional[ReliabilityRates] = None,
) -> float:
    """Higher is better: burn the fat wallets first, then derate flaky ones.

    score = hours_remaining * quota_frac * rate_24h * rate_1h

    Unknown reliability stays 1.0 so a quiet provider is not punished.
    Hours remaining floor at one minute so a nearly-reset provider with
    leftover quota still ranks above a true empty wallet.
    """
    hours = max(float(reading.reset_seconds) / 3600.0, MIN_HOURS_REMAINING)
    quota_frac = max(0.0, min(float(reading.pct) / 100.0, 1.0))
    resolved = rates or ReliabilityRates()
    return hours * quota_frac * resolved.rate_24h * resolved.rate_1h


def compute_desired_order(
    entries: Sequence[Mapping[str, Any]],
    readings_by_provider: Mapping[str, QuotaReading],
    reliability: Optional[Mapping[str, ReliabilityRates]] = None,
) -> List[dict]:
    if not entries:
        return []

    rates = reliability or {}
    indexed = list(enumerate(entries))
    openrouter: List[Tuple[int, dict]] = []
    scored_healthy: List[Tuple[int, dict, float]] = []
    scored_low: List[Tuple[int, dict, float]] = []
    unscored: List[Tuple[int, dict]] = []

    for index, entry in indexed:
        provider = str(entry.get("provider") or "").strip().lower()
        if provider == "openrouter":
            openrouter.append((index, dict(entry)))
            continue

        reading = readings_by_provider.get(provider)
        if reading is None:
            unscored.append((index, dict(entry)))
            continue

        item = (index, dict(entry), score_provider(reading, rates.get(provider)))
        if reading.pct < LOW_QUOTA_PCT:
            scored_low.append(item)
        else:
            scored_healthy.append(item)

    # highest score first; original index breaks exact ties
    scored_healthy.sort(key=lambda item: (-item[2], item[0]))
    scored_low.sort(key=lambda item: (-item[2], item[0]))

    ordered: List[dict] = [entry for _, entry in openrouter]
    ordered.extend(entry for _, entry, _ in scored_healthy)
    ordered.extend(entry for _, entry, _ in scored_low)
    ordered.extend(entry for _, entry in unscored)
    return ordered


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


def write_fallback_order(
    desired_entries: Sequence[Mapping[str, Any]],
    expected_signature: Tuple[Tuple[str, str, str], ...],
) -> None:
    from hermes_cli.config import load_config, save_config

    config = load_config()
    validate_fallback_entries(config.get("fallback_providers") or [])
    validate_fallback_entries(desired_entries)

    backup_path = backup_config()
    config["fallback_providers"] = [dict(entry) for entry in desired_entries]
    try:
        save_config(config)
        chain = chain_signature(load_config())
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
        names, load_precise_readings(quota_interval_seconds, now_fn=now_fn)
    )

    from hermes_cli.config import load_config

    config = load_config()
    current_entries = [
        dict(entry)
        for entry in (config.get("fallback_providers") or [])
        if isinstance(entry, dict)
    ]
    validate_fallback_entries(current_entries)

    reliability = rates_for_providers(
        (str(entry.get("provider") or "") for entry in current_entries),
        now_fn=now_fn,
    )
    scores = {
        provider: score_provider(reading, reliability.get(provider))
        for provider, reading in readings.items()
    }
    desired_entries = compute_desired_order(
        current_entries, readings, reliability=reliability
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
    would_change = desired_sig != current_sig and not frozen

    result = {
        "names": names,
        "readings": readings,
        "reliability": reliability,
        "scores": scores,
        "current_entries": current_entries,
        "desired_entries": desired_entries,
        "current_signature": current_sig,
        "desired_signature": desired_sig,
        "would_change": would_change,
        "frozen": frozen,
        "consecutive_stale": int(new_state.get("consecutive_stale") or 0),
    }

    if dry_run:
        return result

    save_state(new_state)  # dry-run performs no state or config writes

    if frozen:
        return result

    if desired_sig == current_sig:
        return result

    write_fallback_order(desired_entries, desired_sig)
    return result
