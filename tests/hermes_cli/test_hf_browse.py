"""The HF browser: search the firehose, price it roughly, and let any
GGUF become a normal staged model.

Parsing contracts run against canned HF API shapes (no network); route
parsing and fit-band logic with the HF client stubbed."""

from __future__ import annotations

import pytest

from hermes_cli.local_runtime.estimator import HardwareBudget
from hermes_cli.local_runtime.hf_browse import (
    HFFileGroup,
    HFModelHit,
    repo_files,
    rough_fit,
    search_models,
)

GIB = 1 << 30


def _budget(vram_gib, ram_gib=64):
    return HardwareBudget(usable_vram_bytes=int(vram_gib * GIB),
                          total_device_bytes=int(vram_gib * GIB),
                          ram_available_bytes=int(ram_gib * GIB))


def test_search_parses_hf_hits(monkeypatch):
    canned = [
        {"id": "unsloth/Qwen3.8-27B-GGUF", "downloads": 872724, "likes": 47,
         "lastModified": "2026-08-18", "gated": False},
        {"id": "bartowski/whatever-GGUF", "downloads": 5, "likes": 0,
         "lastModified": "2026-01-01", "gated": "auto"},
    ]
    monkeypatch.setattr("hermes_cli.local_runtime.hf_browse._get_json",
                        lambda url: canned)
    hits = search_models("qwen")
    assert hits[0].repo == "unsloth/Qwen3.8-27B-GGUF"
    assert hits[0].downloads == 872724
    assert hits[1].gated is True  # HF 'auto'-gated counts as gated


def test_repo_files_groups_splits_and_excludes_companions(monkeypatch):
    canned = [
        {"path": "Qwen3.8-27B-Q4_K_M.gguf", "size": 17 * GIB},
        {"path": "mmproj-BF16.gguf", "size": 1 * GIB},
        {"path": "UD-Q8/model-00001-of-00002.gguf", "size": 30 * GIB},
        {"path": "UD-Q8/model-00002-of-00002.gguf", "size": 12 * GIB},
        {"path": "README.md", "size": 1000},
        {"path": "dspark-draft-Q8_0.gguf", "size": 9 * GIB},
    ]
    monkeypatch.setattr("hermes_cli.local_runtime.hf_browse._get_json",
                        lambda url: canned)
    groups = repo_files("any/repo")
    labels = {g.label: g for g in groups}
    assert "Q4_K_M" in labels and labels["Q4_K_M"].total_bytes == 17 * GIB
    # Split parts collapse into one group, ordered, summed.
    split = next(g for g in groups if len(g.paths) == 2)
    assert split.total_bytes == 42 * GIB
    assert split.paths[0].endswith("00001-of-00002.gguf")
    # Companions (mmproj, draft) are not standalone models.
    assert not any("mmproj" in p or "dspark" in p
                   for g in groups for p in g.paths)
    # Largest first.
    assert groups[0].total_bytes >= groups[-1].total_bytes


def test_rough_fit_bands():
    b = _budget(29.6, ram_gib=64)
    assert rough_fit(20 * GIB, b) == "fits-gpu"     # + fill-ins under 29.6
    assert rough_fit(28 * GIB, b) == "needs-ram"    # weights spill
    assert rough_fit(120 * GIB, b) == "too-big"
