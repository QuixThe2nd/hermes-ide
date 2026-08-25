"""Reliability ledger contracts for fallback ranking."""

from __future__ import annotations

from pathlib import Path

from plugins.fallback_quota_reorder.reliability import (
    MIN_SAMPLES_1H,
    MIN_SAMPLES_24H,
    NEUTRAL_RATE,
    record_outcome,
    rates_for_providers,
)


NOW = 2_000_000.0


def test_empty_ledger_is_neutral(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    rates = rates_for_providers(["xai-oauth"], now_fn=lambda: NOW, path=path)
    assert rates["xai-oauth"].rate_24h == NEUTRAL_RATE
    assert rates["xai-oauth"].rate_1h == NEUTRAL_RATE
    assert rates["xai-oauth"].samples_24h == 0


def test_below_min_samples_stays_neutral(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    record_outcome("xai-oauth", False, ts=NOW - 10, now_fn=lambda: NOW, path=path)
    rates = rates_for_providers(["xai-oauth"], now_fn=lambda: NOW, path=path)
    assert rates["xai-oauth"].samples_24h == 1
    assert rates["xai-oauth"].rate_24h == NEUTRAL_RATE
    assert rates["xai-oauth"].rate_1h == NEUTRAL_RATE


def test_windows_compute_separately(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    # 24h window: 4 ok + 1 fail. 1h window: 1 ok + 1 fail.
    stamps = [
        (NOW - 20 * 3600, True),
        (NOW - 18 * 3600, True),
        (NOW - 10 * 3600, True),
        (NOW - 90, False),
        (NOW - 10, True),
    ]
    for ts, ok in stamps:
        record_outcome("xai-oauth", ok, ts=ts, now_fn=lambda: NOW, path=path)

    rates = rates_for_providers(["xai-oauth"], now_fn=lambda: NOW, path=path)
    assert rates["xai-oauth"].samples_24h == 5
    assert rates["xai-oauth"].successes_24h == 4
    assert rates["xai-oauth"].rate_24h == 0.8
    assert rates["xai-oauth"].samples_1h == 2
    assert rates["xai-oauth"].successes_1h == 1
    assert rates["xai-oauth"].rate_1h == 0.5
    assert MIN_SAMPLES_24H == 3
    assert MIN_SAMPLES_1H == 2


def test_events_older_than_24h_are_ignored(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    record_outcome("xai-oauth", False, ts=NOW - 25 * 3600, now_fn=lambda: NOW, path=path)
    for offset in (10, 20, 30):
        record_outcome("xai-oauth", True, ts=NOW - offset, now_fn=lambda: NOW, path=path)
    rates = rates_for_providers(["xai-oauth"], now_fn=lambda: NOW, path=path)
    assert rates["xai-oauth"].samples_24h == 3
    assert rates["xai-oauth"].rate_24h == 1.0


def test_blank_provider_is_dropped(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    record_outcome("  ", True, ts=NOW, now_fn=lambda: NOW, path=path)
    assert not path.exists()
