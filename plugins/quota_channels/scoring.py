"""Spendability scoring for the Discord Models quota channels."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from plugins.quota_channels.reliability import ReliabilityRates

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
RESET_CREDIT_PROVIDERS = frozenset({"openai-codex", "xai-oauth", "zai"})

LOW_QUOTA_PCT = 5
MIN_HOURS_REMAINING = 1.0 / 60.0  # 1 minute; zero hours would divide by zero
REFERENCE_HOURS = 168.0  # one week — the score's neutral time horizon


@dataclass(frozen=True)
class QuotaReading:
    channel_key: str
    provider: str
    channel_name: str
    pct: int
    reset_seconds: float
    # pending manual usage-limit resets (providers with a resets API only —
    # Codex/Grok/z.ai). ``reset_count`` of
    # 0 means no extra wallets; ``reset_expiry_seconds`` of None means the
    # expiry is unknown, so the credit stays counted but scores nothing —
    # the usage-reset countdown is never borrowed as a stand-in.
    reset_count: int = 0
    reset_expiry_seconds: Optional[float] = None
    # each counted credit's own future expiry, when the richer per-credit
    # state exists. None/empty means it does not, so the single
    # ``reset_expiry_seconds`` clock keeps its legacy whole-stack meaning;
    # a non-empty list scores every credit on its own clock exactly once and
    # never multiplies one clock by the count.
    reset_expiry_horizons: Optional[Tuple[float, ...]] = None


def sanitize_reset_expiry_horizons(raw: Any) -> Tuple[float, ...]:
    """Scoreable per-credit reset-expiry horizons (seconds) from ``raw``.

    Each entry must be a positive finite number; malformed entries —
    non-numeric, non-finite, or non-positive values — are dropped, never
    fatal, so a hand-edited or half-written state row cannot fail the tick.
    Anything that is not a list/tuple at all sanitizes to an empty tuple,
    which callers treat as "no richer list exists".
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    horizons: List[float] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        horizon = float(value)
        if not math.isfinite(horizon) or horizon <= 0.0:
            continue
        horizons.append(horizon)
    return tuple(horizons)


def _reset_credit_count(reading: QuotaReading) -> int:
    """The pending-reset count that actually scores for ``reading``.

    Contract gate: only providers with a resets API
    (``RESET_CREDIT_PROVIDERS`` — Codex/Grok/z.ai) have reset credits, so a
    reset field injected into any other provider's reading counts as zero
    everywhere: no score term, no low-quota escape.
    """
    if str(reading.provider).strip().lower() not in RESET_CREDIT_PROVIDERS:
        return 0
    return max(0, int(reading.reset_count))


def _scored_reset_horizons(reading: QuotaReading) -> Tuple[float, ...]:
    """The per-credit expiry horizons that score for ``reading``.

    Same contract gate as ``_reset_credit_count``: horizons injected into a
    provider without a resets API are inert. Malformed entries are dropped
    by ``sanitize_reset_expiry_horizons`` rather than trusted.
    """
    if str(reading.provider).strip().lower() not in RESET_CREDIT_PROVIDERS:
        return ()
    return sanitize_reset_expiry_horizons(reading.reset_expiry_horizons)


def score_provider(
    reading: QuotaReading,
    rates: Optional[ReliabilityRates] = None,
) -> float:
    """Higher is better: spend the soonest-reset wallets first, derate flaky ones.

    score = (remaining_term + reset_term) * rate_24h * rate_1h
    remaining_term = quota_frac * (REFERENCE_HOURS / hours_remaining)
    reset_term     = sum over credits of REFERENCE_HOURS / hours_credit_expires

    Time enters inversely: the sooner a wallet refills, the more urgent it
    is to burn it now, while a wallet resetting in exactly REFERENCE_HOURS
    (or later) scores its quota fraction 1:1 or less. Unknown reliability
    stays 1.0 so a quiet provider is not punished. Hours remaining floor at
    one minute so a nearly-reset wallet is not divided by zero.

    Each pending manual usage-limit reset adds its own full wallet
    (quota fraction 1.0) on its own expiry clock — additive and stackable,
    with no cap. When the richer per-credit horizons are known
    (``reset_expiry_horizons``), every credit is scored exactly once on its
    own clock: credits expiring in 3d and 9d score 168/72 + 168/216, never
    2 * 168/72. A credit whose expiry is unknown is simply absent from that
    list: it stays counted (``reset_count``) and lifts the wallet out of the
    low-quota sink, but adds nothing — urgency is not measurable, so no
    other clock is borrowed as a stand-in. Without a richer list the legacy
    single-clock shape keeps its meaning: ``reset_count`` credits all on the
    one ``reset_expiry_seconds`` clock, which is exactly what a channel name
    or a pre-list state row reports. Only Codex/Grok/z.ai have a resets API
    at all — reset fields on any other provider are inert (see
    ``_reset_credit_count`` and ``_scored_reset_horizons``).
    """
    hours = max(float(reading.reset_seconds) / 3600.0, MIN_HOURS_REMAINING)
    quota_frac = max(0.0, min(float(reading.pct) / 100.0, 1.0))
    resolved = rates or ReliabilityRates()
    remaining_term = quota_frac * (REFERENCE_HOURS / hours)
    horizons = _scored_reset_horizons(reading)
    reset_term = 0.0
    if horizons:
        reset_term = sum(
            REFERENCE_HOURS / max(horizon / 3600.0, MIN_HOURS_REMAINING)
            for horizon in horizons
        )
    else:
        reset_count = _reset_credit_count(reading)
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
    for Codex/Grok/z.ai — an injected count on any other provider sinks with
    the wallet it polluted.
    """
    return reading.pct < LOW_QUOTA_PCT and _reset_credit_count(reading) <= 0
