"""Rolling per-provider API success ledger used by fallback ranking.

Hooks append one compact event per provider request. Ranking reads the last
24h and last 1h windows. The file is JSONL so a crash mid-write loses at
most one event; prune keeps it bounded.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional

NowFn = Callable[[], float]

LEDGER_FILENAME = "fallback_quota_reorder_reliability.jsonl"
WINDOW_24H_SECONDS = 24 * 3600
WINDOW_1H_SECONDS = 3600
PRUNE_EVERY_SECONDS = 300
MAX_EVENTS = 20_000
MIN_SAMPLES_24H = 3
MIN_SAMPLES_1H = 2
NEUTRAL_RATE = 1.0


@dataclass(frozen=True)
class ReliabilityRates:
    """Success fractions for one provider. Missing windows stay neutral."""

    rate_24h: float = NEUTRAL_RATE
    rate_1h: float = NEUTRAL_RATE
    samples_24h: int = 0
    samples_1h: int = 0
    successes_24h: int = 0
    successes_1h: int = 0


def ledger_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / LEDGER_FILENAME


def _clamp_rate(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _normalize_provider(provider: object) -> str:
    return str(provider or "").strip().lower()


def _as_float(value: object, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _safe_ts(value: object, fallback: float) -> float:
    ts = _as_float(value, fallback)
    if ts <= 0:
        return fallback
    return ts


def _iter_events(path: Path) -> Iterable[dict]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                yield event


def record_outcome(
    provider: str,
    success: bool,
    *,
    ts: Optional[float] = None,
    now_fn: NowFn = time.time,
    path: Optional[Path] = None,
) -> None:
    """Append one success/fail event. Never raises into the hook path."""
    slug = _normalize_provider(provider)
    if not slug:
        return
    dest = path or ledger_path()
    stamp = ts if ts is not None else now_fn()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"ts": float(stamp), "provider": slug, "ok": bool(success)},
                    separators=(",", ":"),
                )
                + "\n"
            )
    except OSError:
        return
    try:
        _maybe_prune(dest, now_fn=now_fn)
    except Exception:
        return


def _maybe_prune(path: Path, *, now_fn: NowFn) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    if stat.st_size < 64_000 and (now_fn() - stat.st_mtime) < PRUNE_EVERY_SECONDS:
        return

    cutoff = now_fn() - WINDOW_24H_SECONDS
    kept: list[str] = []
    for event in _iter_events(path):
        ts = _safe_ts(event.get("ts"), 0.0)
        if ts < cutoff:
            continue
        kept.append(
            json.dumps(
                {
                    "ts": ts,
                    "provider": _normalize_provider(event.get("provider")),
                    "ok": bool(event.get("ok")),
                },
                separators=(",", ":"),
            )
        )
    if len(kept) > MAX_EVENTS:
        kept = kept[-MAX_EVENTS:]

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".reliability.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept))
            if kept:
                handle.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def rates_for_providers(
    providers: Iterable[str],
    *,
    now_fn: NowFn = time.time,
    path: Optional[Path] = None,
) -> Dict[str, ReliabilityRates]:
    now = now_fn()
    cutoff_24h = now - WINDOW_24H_SECONDS
    cutoff_1h = now - WINDOW_1H_SECONDS
    wanted = {_normalize_provider(p) for p in providers if _normalize_provider(p)}
    totals: Dict[str, list[int]] = {p: [0, 0, 0, 0] for p in wanted}

    dest = path or ledger_path()
    for event in _iter_events(dest):
        slug = _normalize_provider(event.get("provider"))
        if slug not in wanted:
            continue
        ts = _safe_ts(event.get("ts"), 0.0)
        if ts < cutoff_24h:
            continue
        ok = 1 if event.get("ok") else 0
        row = totals[slug]
        row[0] += 1
        row[1] += ok
        if ts >= cutoff_1h:
            row[2] += 1
            row[3] += ok

    result: Dict[str, ReliabilityRates] = {}
    for slug in wanted:
        samples_24h, successes_24h, samples_1h, successes_1h = totals[slug]
        rate_24h = (
            successes_24h / samples_24h
            if samples_24h >= MIN_SAMPLES_24H
            else NEUTRAL_RATE
        )
        rate_1h = (
            successes_1h / samples_1h
            if samples_1h >= MIN_SAMPLES_1H
            else NEUTRAL_RATE
        )
        result[slug] = ReliabilityRates(
            rate_24h=_clamp_rate(rate_24h),
            rate_1h=_clamp_rate(rate_1h),
            samples_24h=samples_24h,
            samples_1h=samples_1h,
            successes_24h=successes_24h,
            successes_1h=successes_1h,
        )
    return result


def rates_from_mapping(
    raw: Optional[Mapping[str, Mapping[str, object]]],
) -> Dict[str, ReliabilityRates]:
    """Test helper: build rates without touching the ledger file."""
    if not raw:
        return {}
    out: Dict[str, ReliabilityRates] = {}
    for provider, payload in raw.items():
        slug = _normalize_provider(provider)
        if not slug or not isinstance(payload, Mapping):
            continue
        rate_24h = _as_float(payload.get("rate_24h"), NEUTRAL_RATE)
        rate_1h = _as_float(payload.get("rate_1h"), NEUTRAL_RATE)
        out[slug] = ReliabilityRates(
            rate_24h=_clamp_rate(rate_24h),
            rate_1h=_clamp_rate(rate_1h),
            samples_24h=_as_int(payload.get("samples_24h")),
            samples_1h=_as_int(payload.get("samples_1h")),
            successes_24h=_as_int(payload.get("successes_24h")),
            successes_1h=_as_int(payload.get("successes_1h")),
        )
    return out
