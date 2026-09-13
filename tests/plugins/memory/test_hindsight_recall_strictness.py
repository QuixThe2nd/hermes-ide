"""Recall strictness knobs for the Hindsight memory provider (hindsight-client >= 0.9.2).

``recall_min_scores`` — server-side inclusive AND-ed per-stage score floors passed
straight through to ``arecall(min_scores=...)``; ``recall_max_results`` — client-side
top-N cap applied after floor filtering. Unset knobs must reproduce the exact
pre-knob arecall request (no ``min_scores`` key, unmodified result handling).
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.memory.hindsight import HindsightMemoryProvider


# Server-emulating fake results, ranked by final score descending (the recall
# API's documented ordering — the client-side cap relies on it).
def _scored(text, final):
    return SimpleNamespace(text=text, scores=SimpleNamespace(final=final))


_RANKED = [
    _scored("alpha memory", 0.9),
    _scored("beta memory", 0.7),
    _scored("gamma memory", 0.35),
    _scored("delta memory", 0.1),
]


def _server_like_client(results=None):
    """Mock client whose arecall applies the documented server floor semantics:
    results whose ``scores.final`` falls below ``min_scores["final"]`` are dropped."""
    results = list(_RANKED if results is None else results)

    async def _arecall(**kwargs):
        floors = kwargs.get("min_scores") or {}
        final_floor = floors.get("final")
        kept = [r for r in results if final_floor is None or r.scores.final >= final_floor]
        return SimpleNamespace(results=kept)

    client = MagicMock()
    client.arecall = AsyncMock(side_effect=_arecall)
    return client


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    for key in (
        "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL", "HINDSIGHT_BANK_ID",
        "HINDSIGHT_BUDGET", "HINDSIGHT_MODE", "HINDSIGHT_TIMEOUT",
        "HINDSIGHT_IDLE_TIMEOUT", "HINDSIGHT_LLM_API_KEY",
        "HINDSIGHT_RETAIN_TAGS", "HINDSIGHT_RETAIN_OBSERVATION_SCOPES",
        "HINDSIGHT_RETAIN_SOURCE",
        "HINDSIGHT_RETAIN_USER_PREFIX", "HINDSIGHT_RETAIN_ASSISTANT_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))


@pytest.fixture()
def make_provider(tmp_path, monkeypatch):
    """Factory writing a hindsight config.json with *overrides* and returning an
    initialized provider wired to a fresh server-like mock client."""
    def _make(**overrides):
        config = {
            "mode": "cloud",
            "apiKey": "test-key",
            "api_url": "http://localhost:9999",
            "bank_id": "test-bank",
            "budget": "mid",
            "memory_mode": "hybrid",
        }
        config.update(overrides)
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr(
            "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
        )
        p = HindsightMemoryProvider()
        p.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
        p._client = _server_like_client()
        return p
    return _make


# ---------------------------------------------------------------------------
# recall_min_scores — floors reach the server, results below them disappear
# ---------------------------------------------------------------------------


class TestMinScores:
    def test_dict_config_passed_straight_to_arecall(self, make_provider):
        p = make_provider(recall_min_scores={"semantic": 0.2, "final": 0.5})

        p._do_recall("dark mode")

        assert p._client.arecall.call_args.kwargs["min_scores"] == {
            "semantic": 0.2, "final": 0.5,
        }

    def test_json_string_config_parses_to_same_request(self, make_provider):
        p = make_provider(recall_min_scores='{"semantic": 0.2, "final": 0.5}')

        p._do_recall("dark mode")

        assert p._client.arecall.call_args.kwargs["min_scores"] == {
            "semantic": 0.2, "final": 0.5,
        }

    def test_results_below_floor_are_dropped_from_recalled_context(self, make_provider):
        """End to end against a fake that applies the server's floor semantics:
        only at-or-above-floor memories may reach the injected context."""
        p = make_provider(recall_min_scores={"final": 0.4})

        text, count = p._do_recall("dark mode")

        assert "alpha memory" in text and "beta memory" in text
        assert "gamma memory" not in text and "delta memory" not in text
        assert count == 2

    def test_floor_is_inclusive(self, make_provider):
        p = make_provider(recall_min_scores={"final": 0.35})

        _, count = p._do_recall("dark mode")

        # gamma (0.35) sits exactly on the floor and survives.
        assert count == 3

    def test_tool_recall_path_receives_floors_too(self, make_provider):
        """hindsight_recall routes through _recall(), so one wiring point covers
        both auto-recall and the manual tool."""
        p = make_provider(recall_min_scores={"final": 0.4})

        result = json.loads(p.handle_tool_call("hindsight_recall", {"query": "dark mode"}))

        assert "min_scores" in p._client.arecall.call_args.kwargs
        assert "gamma memory" not in result["result"]
        assert "alpha memory" in result["result"]

    @pytest.mark.parametrize("empty", [None, "", {}])
    def test_unset_or_empty_means_no_floors(self, make_provider, empty):
        p = make_provider(recall_min_scores=empty)

        assert p._recall_min_scores is None
        p._do_recall("dark mode")
        assert "min_scores" not in p._client.arecall.call_args.kwargs


# ---------------------------------------------------------------------------
# recall_max_results — client-side top-N cap after floor filtering
# ---------------------------------------------------------------------------


class TestMaxResults:
    def test_cap_keeps_top_n_ranked_results(self, make_provider):
        p = make_provider(recall_max_results=2)

        text, count = p._do_recall("dark mode")

        assert "alpha memory" in text and "beta memory" in text
        assert "gamma memory" not in text and "delta memory" not in text
        assert count == 2

    def test_cap_applies_after_floor_filtering(self, make_provider):
        """Floors drop the weak tail first; the cap then keeps the top N of the
        survivors, not of the raw server result set."""
        p = make_provider(recall_min_scores={"final": 0.4}, recall_max_results=1)

        text, count = p._do_recall("dark mode")

        assert "alpha memory" in text
        assert "beta memory" not in text  # survived the floor, cut by the cap
        assert count == 1

    def test_cap_is_client_side_not_a_request_kwarg(self, make_provider):
        """The recall API has no server-side max-results param — the cap must
        never leak into the arecall request."""
        p = make_provider(recall_max_results=1)

        p._do_recall("dark mode")

        assert "max_results" not in p._client.arecall.call_args.kwargs
        assert "limit" not in p._client.arecall.call_args.kwargs

    def test_cap_larger_than_result_set_is_a_noop(self, make_provider):
        p = make_provider(recall_max_results=50)

        _, count = p._do_recall("dark mode")

        assert count == len(_RANKED)

    @pytest.mark.parametrize("no_cap", [None, 0])
    def test_unset_or_zero_means_no_cap(self, make_provider, no_cap):
        p = make_provider(recall_max_results=no_cap)

        assert p._recall_max_results == 0
        _, count = p._do_recall("dark mode")
        assert count == len(_RANKED)


# ---------------------------------------------------------------------------
# Unset knobs — byte-identical arecall request and unmodified result handling
# ---------------------------------------------------------------------------


class TestUnsetKnobsPreserveBehavior:
    def test_arecall_request_is_byte_identical_without_knobs(self, make_provider):
        p = make_provider()

        p._do_recall("dark mode")

        p._client.arecall.assert_called_once_with(**{
            "bank_id": "test-bank",
            "query": "dark mode",
            "budget": "mid",
            "max_tokens": 4096,
            "types": ["observation"],
        })

    def test_all_results_surface_without_knobs(self, make_provider):
        p = make_provider()

        text, count = p._do_recall("dark mode")

        assert count == len(_RANKED)
        for r in _RANKED:
            assert r.text in text


# ---------------------------------------------------------------------------
# Invalid recall_min_scores — warn and still recall
# ---------------------------------------------------------------------------


class TestInvalidMinScores:
    @pytest.mark.parametrize("bad", ['{"final": oops}', "[1, 2]", '"floors"', "not json"])
    def test_invalid_config_warns_once_and_recalls_unfloored(self, make_provider, caplog, bad):
        with caplog.at_level(logging.WARNING):
            p = make_provider(recall_min_scores=bad)

        assert any("recall_min_scores" in r.getMessage() for r in caplog.records)
        assert p._recall_min_scores is None

        text, count = p._do_recall("dark mode")
        assert count == len(_RANKED)  # recall succeeded, nothing dropped
        assert "min_scores" not in p._client.arecall.call_args.kwargs

    def test_non_numeric_floor_warns_and_behaves_unset(self, make_provider, caplog):
        with caplog.at_level(logging.WARNING):
            p = make_provider(recall_min_scores={"final": "high"})

        assert any("recall_min_scores" in r.getMessage() for r in caplog.records)
        p._do_recall("dark mode")
        assert "min_scores" not in p._client.arecall.call_args.kwargs


# ---------------------------------------------------------------------------
# Config schema exposure
# ---------------------------------------------------------------------------


class TestSchemaExposesKnobs:
    def test_schema_lists_both_knobs_with_safe_defaults(self):
        schema = HindsightMemoryProvider().get_config_schema()
        by_key = {f["key"]: f for f in schema}

        assert by_key["recall_min_scores"]["default"] == ""
        assert "final" in by_key["recall_min_scores"]["description"]
        assert by_key["recall_max_results"]["default"] == 0
