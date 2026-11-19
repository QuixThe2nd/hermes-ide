"""Optional-skills catalog composition after the mlops strip.

The mlops category (36 skills) was removed from this fork: no hub source
offers it and no doc lists it as a shipped optional category. This guards
against silent reintroduction — e.g. a merge from a tree that still ships
it — which would re-offer skills whose supporting docs and tests were
deleted in the same change.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_mlops_category_absent_from_optional_catalog():
    assert not (REPO / "optional-skills" / "mlops").exists(), (
        "optional-skills/mlops was removed from this fork; if it is back, "
        "reinstate the category lists, docs, and tests that offer it in the "
        "same change"
    )
