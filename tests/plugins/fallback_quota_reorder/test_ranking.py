"""Desired fallback order ranking contracts."""

from __future__ import annotations

from plugins.fallback_quota_reorder.core import QuotaReading, compute_desired_order


def _reading(provider: str, pct: int, reset_seconds: int, channel_key: str) -> QuotaReading:
    return QuotaReading(
        channel_key=channel_key,
        provider=provider,
        channel_name="unused",
        pct=pct,
        reset_seconds=reset_seconds,
    )


class TestComputeDesiredOrder:
    def test_openrouter_always_first(self):
        entries = [
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "openrouter", "model": "or"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        readings = {
            "xai-oauth": _reading("xai-oauth", 90, 3600, "grok"),
            "kimi-coding": _reading("kimi-coding", 90, 1800, "kimi"),
        }
        ordered = compute_desired_order(entries, readings)
        assert ordered[0]["provider"] == "openrouter"

    def test_healthy_entries_sort_by_soonest_reset(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "kimi-coding", "model": "kimi"},
        ]
        readings = {
            "openai-codex": _reading("openai-codex", 80, 86400, "codex"),
            "xai-oauth": _reading("xai-oauth", 70, 3600, "grok"),
            "kimi-coding": _reading("kimi-coding", 60, 7200, "kimi"),
        }
        ordered = compute_desired_order(entries, readings)
        providers = [entry["provider"] for entry in ordered]
        assert providers.index("openrouter") < providers.index("xai-oauth")
        assert providers.index("xai-oauth") < providers.index("kimi-coding")
        assert providers.index("kimi-coding") < providers.index("openai-codex")

    def test_low_pct_sinks_behind_all_healthy_entries(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "zai", "model": "zai"},
            {"provider": "xai-oauth", "model": "grok"},
        ]
        readings = {
            "zai": _reading("zai", 3, 1800, "zai"),
            "xai-oauth": _reading("xai-oauth", 50, 7200, "grok"),
        }
        ordered = compute_desired_order(entries, readings)
        providers = [entry["provider"] for entry in ordered]
        assert providers.index("xai-oauth") < providers.index("zai")

    def test_unreadable_entries_keep_relative_tail_order(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "xai-oauth", "model": "grok"},
            {"provider": "custom-a", "model": "a"},
            {"provider": "custom-b", "model": "b"},
        ]
        readings = {
            "xai-oauth": _reading("xai-oauth", 90, 3600, "grok"),
        }
        ordered = compute_desired_order(entries, readings)
        providers = [entry["provider"] for entry in ordered]
        assert providers == ["openrouter", "xai-oauth", "custom-a", "custom-b"]

    def test_stability_preserves_original_index_on_ties(self):
        entries = [
            {"provider": "openrouter", "model": "or"},
            {"provider": "openai-codex", "model": "codex-a"},
            {"provider": "xai-oauth", "model": "grok-a"},
            {"provider": "kimi-coding", "model": "kimi-a"},
            {"provider": "zai", "model": "zai-a"},
        ]
        reset = 7200
        readings = {
            "openai-codex": _reading("openai-codex", 90, reset, "codex"),
            "xai-oauth": _reading("xai-oauth", 90, reset, "grok"),
            "kimi-coding": _reading("kimi-coding", 90, reset, "kimi"),
            "zai": _reading("zai", 90, reset, "zai"),
        }
        ordered = compute_desired_order(entries, readings)
        tied = [
            entry["provider"]
            for entry in ordered
            if entry["provider"] != "openrouter"
        ]
        assert tied == ["openai-codex", "xai-oauth", "kimi-coding", "zai"]
