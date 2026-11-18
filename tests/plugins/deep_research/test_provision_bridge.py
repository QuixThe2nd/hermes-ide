"""Runtime-bridge contract for worker-profile provisioning decisions.

``plugins/deep_research/provision.py`` contains NO decision table: it
gathers runtime facts and pipes each decision record through the canonical
Go table (``internal/profileprovision``, executed as
``cmd/profileprovision``). These tests run the real bridge — the binary
resolved/built exactly as the runtime resolves it — against the shared
table in ``internal/profileprovision/testdata/provision_cases.json``,
which the Go tests also execute. The degraded path (no binary) is pinned
too: it must make the same observable decision for every well-formed name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.deep_research import provision

_TABLE = (
    Path(__file__).resolve().parents[3] / "internal" / "profileprovision" / "testdata" / "provision_cases.json"
)

requires_go = pytest.mark.skipif(
    provision._decision_binary() is None,
    reason="provisioning decisions need the Go binary (cached or built from the checkout)",
)


@requires_go
@pytest.mark.parametrize("case", json.loads(_TABLE.read_text(encoding="utf-8"))["cases"], ids=lambda case: case["name"])
def test_bridge_decides_the_shared_table(case):
    assert provision._decide(case["record"]) == case["expect"]


def test_fallback_decides_like_the_table_without_a_binary(monkeypatch):
    """No binary → the degraded path: same observable decision per well-formed name."""
    monkeypatch.setattr(provision, "_decision_binary", lambda: None)
    assert provision._decide(
        {"op": "plan", "name": "researcher", "exists": False, "active_profile": "default", "active_is_named": False}
    ) == {"action": "create", "clone_config": True, "clone_from": ""}
    assert provision._decide(
        {"op": "plan", "name": "researcher", "exists": True, "active_profile": "default", "active_is_named": False}
    ) == {"action": "skip"}
    verdict = provision._decide(
        {"op": "classify", "name": "researcher", "race": False, "error": "profiles root is read-only"}
    )
    assert verdict["outcome"] == "failed"
    assert verdict["code"] == "worker_profile_provision_failed"
    assert "researcher" in verdict["message"]
    assert provision._decide({"op": "classify", "name": "researcher", "race": True, "error": ""}) == {
        "outcome": "provisioned"
    }
