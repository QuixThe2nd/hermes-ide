"""Regression tests for home-skill discovery and the optional-skill catalog.

Covers two user-facing behavior contracts after the telephony strip
(branch strip/telephony):

1. A skill installed in the active home (e.g. the ``shop`` skill) is
   discovered as enabled through the real ``skills_list`` /
   ``skill_view`` path, and the normal config gate
   (``skills.disabled``) removes it — the enable/disable mechanics,
   not a hardcoded catalog snapshot.
2. The removed optional ``telephony`` skill is no longer exposed by
   catalog discovery (``OptionalSkillSource`` search/list/fetch),
   with all network fallbacks mocked offline.

All skills used here are benign FIXTURE content written into a temp
``HERMES_HOME`` — nothing from the user's real home is copied in.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

ROOT = Path(__file__).resolve().parents[2]

_SHOP_FIXTURE = """---
name: shop
description: "Fixture shop catalog skill for isolated discovery tests."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
---

# Shop (fixture)

Search a fixture catalog and track fixture orders.

- SHOP-FIXTURE-MARKER: look up items by query, report price and availability.
"""


@pytest.fixture
def fixture_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with a fixture ``shop`` skill installed under a category dir.

    ``tools.skills_tool`` snapshots the skills dir at import time, so the
    module-level constant is patched alongside the env var (same pattern as
    tests/cron/test_cron_prompt_injection_skill.py).
    """
    home = tmp_path / ".hermes"
    skills_dir = home / "skills"
    shop_dir = skills_dir / "productivity" / "shop"
    shop_dir.mkdir(parents=True)
    (shop_dir / "SKILL.md").write_text(_SHOP_FIXTURE, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.skills_tool as skills_tool
    monkeypatch.setattr(skills_tool, "HERMES_HOME", home)
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills_dir)
    skills_tool._SKILLS_CACHE.clear()

    yield home

    skills_tool._SKILLS_CACHE.clear()


class TestInstalledHomeSkillDiscovery:
    """An installed home skill flows through the real discovery + gate path."""

    def test_installed_shop_skill_discovers_as_enabled(self, fixture_home):
        from tools.skills_tool import skill_view, skills_list

        listing = json.loads(skills_list())
        assert listing["success"], listing
        names = [s["name"] for s in listing["skills"]]
        assert "shop" in names, (
            "A skill installed under <home>/skills/productivity/shop must be "
            f"discovered by skills_list; got {names}"
        )

        viewed = json.loads(skill_view("shop"))
        assert viewed["success"], viewed
        assert "SHOP-FIXTURE-MARKER" in viewed["content"]

    def test_disabled_gate_removes_installed_skill(self, fixture_home):
        """The normal skills.disabled config gate hides the skill from both surfaces."""
        (fixture_home / "config.yaml").write_text(
            "skills:\n  disabled:\n    - shop\n", encoding="utf-8"
        )
        from agent import skill_utils
        skill_utils._raw_config_cache_clear()
        from tools.skills_tool import skill_view, skills_list

        try:
            listing = json.loads(skills_list())
            assert listing["success"], listing
            names = [s["name"] for s in listing["skills"]]
            assert "shop" not in names, (
                "skills.disabled must remove the skill from skills_list"
            )

            viewed = json.loads(skill_view("shop"))
            assert not viewed["success"], viewed
            assert "disabled" in (viewed.get("error") or "").lower()
        finally:
            skill_utils._raw_config_cache_clear()


class TestRemovedTelephonyNotInCatalog:
    """The stripped optional telephony skill is not reachable via catalog discovery.

    Discovery behavior is exercised (local scan + search + fetch) with the
    live-repo network fallbacks mocked offline; the catalog must still be
    non-empty (positive control) so this is not an absence-only assertion.
    """

    def test_telephony_absent_from_local_catalog_discovery(self):
        from tools.skills_hub_official import OptionalSkillSource

        source = OptionalSkillSource()
        source._optional_dir = ROOT / "optional-skills"
        with patch.object(source, "_list_remote_skill_dirs", return_value={}), \
             patch.object(source, "_fetch_from_live_repo", return_value=None):
            local = source.list_local()
            assert local, "optional-skills catalog must still list its shipped skills"

            searched = source.search("telephony")
            for meta in local + searched:
                assert "telephony" not in meta.identifier.lower(), meta.identifier
                assert "telephony" not in meta.name.lower(), meta.name

            assert searched == [], (
                "search('telephony') must return nothing after the skill was removed"
            )
            bundle = source.fetch("official/communication/telephony")
            assert bundle is None, "fetch must not resolve the removed telephony skill"
